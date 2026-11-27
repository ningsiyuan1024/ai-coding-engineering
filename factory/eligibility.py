"""Read public GitHub ownership signals before proposing a factory task."""

import hashlib
import json
import re
import subprocess
from urllib.parse import urlsplit


_COMMENT_LIMIT = 10
_CONNECTION_LIMIT = 50
_SEARCH_LIMIT = 100
_LINK = re.compile(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/([1-9][0-9]*)/?")
_TITLE_PREFIX = re.compile(r"^[\w.-]+/[\w.-]+\s+#\d+\s*[·—:]\s*")
_ISSUE_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner,name:$repo) {
    isPrivate nameWithOwner
    issue(number:$number) {
      id number url state title bodyText updatedAt
      assignees(first:50) { totalCount nodes { login } pageInfo { hasNextPage } }
      comments(last:10) { totalCount nodes { id url author { login } bodyText createdAt updatedAt } }
      closedByPullRequestsReferences(first:50,includeClosedPrs:false) {
        totalCount pageInfo { hasNextPage } nodes { ...OpenPullData }
      }
      timelineItems(last:50,itemTypes:[CROSS_REFERENCED_EVENT, CONNECTED_EVENT, DISCONNECTED_EVENT]) {
        totalCount pageInfo { hasPreviousPage }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            willCloseTarget source { __typename ...OpenPullData }
          }
          ... on ConnectedEvent {
            source { __typename ...OpenPullData } subject { __typename ...OpenPullData }
          }
          ... on DisconnectedEvent {
            source { __typename ...OpenPullData } subject { __typename ...OpenPullData }
          }
        }
      }
    }
  }
}
fragment OpenPullData on PullRequest {
  url number title state updatedAt repository { nameWithOwner isPrivate }
}
"""


class IncompleteEvidence(ValueError):
    """An upstream ownership read did not establish a complete observation."""


def _api(*args):
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", *args],
            capture_output=True, encoding="utf-8", timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IncompleteEvidence("github_request_unavailable") from exc
    if result.returncode != 0:
        reason = "github_rate_limited" if "rate limit" in result.stderr.lower() else "github_request_unsuccessful"
        raise IncompleteEvidence(reason)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise IncompleteEvidence("github_invalid_response") from exc
    if not isinstance(value, dict) or value.get("errors"):
        raise IncompleteEvidence("github_invalid_response")
    return value


def _link(url, kind):
    parsed = urlsplit(url)
    match = _LINK.fullmatch(parsed.path)
    if (
        parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
        or parsed.query or parsed.fragment or match is None or match[2] != kind
        or any(part in (".", "..") for part in match[1].split("/"))
    ):
        raise IncompleteEvidence("github_expected_canonical_link")
    return match[1].lower(), int(match[3])


def _nodes(connection, limit, page_flag=None):
    nodes = connection["nodes"]
    total = connection["totalCount"]
    if (
        type(total) is not int or total < 0 or not isinstance(nodes, list)
        or len(nodes) != min(total, limit) or any(not isinstance(node, dict) for node in nodes)
        or (page_flag is not None and (connection["pageInfo"][page_flag] is not False or total > limit))
    ):
        raise IncompleteEvidence("github_incomplete_connection")
    return nodes


def _compact(text):
    return " ".join(text.split())


def _words(text):
    return " ".join(re.findall(r"\w+", text))


def _has_phrase(text, phrase):
    return bool(phrase) and f" {_words(phrase).casefold()} " in f" {_words(text).casefold()} "


def _scope(task, title, body):
    label = _TITLE_PREFIX.sub("", task.get("title", ""))
    # Use local scope only when its terms already appear in this public issue.
    if _words(label) and _has_phrase(f"{title} {body}", label):
        return _compact(label)
    return _compact(title)


def _excerpt(text, focus="", limit=560):
    compact = _compact(text)
    if len(compact) <= limit:
        return compact
    index = compact.casefold().find(focus.casefold()) if focus else -1
    start = max(0, index - 90)
    end = start + limit
    return ("…" if start else "") + compact[start:end] + ("…" if end < len(compact) else "")


def _search(repo, term):
    response = _api(
        "--method", "GET", "search/issues",
        "-f", f"q=repo:{repo} is:pr is:open in:title,body {term}",
        "-F", f"per_page={_SEARCH_LIMIT}",
    )
    results = response["items"]
    total = response["total_count"]
    if (
        response["incomplete_results"] is not False or type(total) is not int
        or not isinstance(results, list) or len(results) != total or total > _SEARCH_LIMIT
    ):
        raise IncompleteEvidence("github_incomplete_pull_search")
    return results


def _closes_issue(body, repo, number):
    words = r"clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed)"
    target = rf"(?:(?:{re.escape(repo)})?\#{number}|https://github\.com/{re.escape(repo)}/issues/{number})"
    return bool(re.search(rf"\b(?:{words})\s*:?\s+{target}(?![\w/-])", body, re.IGNORECASE))


def _open_pulls(issue, repo, number, scope):
    pulls = {}

    def add(pr, relation, competing, searched=False):
        if searched:
            url, state, updated = pr["html_url"], pr["state"], pr["updated_at"]
            if not isinstance(pr["pull_request"], dict) or _link(url, "pull")[0] != repo or state != "open":
                raise IncompleteEvidence("github_invalid_pull_search_result")
        else:
            url, state, updated = pr["url"], pr["state"], pr["updatedAt"]
            if pr["repository"]["isPrivate"] is not False:
                raise IncompleteEvidence("github_nonpublic_pull_reference")
        key = _link(url, "pull")
        if state.upper() not in ("OPEN", "CLOSED", "MERGED"):
            raise IncompleteEvidence("github_invalid_pull_status")
        if state.upper() == "OPEN":
            current = pulls.setdefault(key, {"url": url, "title": pr["title"], "updated_at": updated,
                                             "relationships": set(), "competing": False})
            current["relationships"].add(relation)
            current["competing"] = current["competing"] or competing
            if searched:
                body = pr["body"] or ""
                current["body_digest"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                current["excerpt"] = _excerpt(body, scope, limit=300)

    linked = _nodes(issue["closedByPullRequestsReferences"], _CONNECTION_LIMIT, "hasNextPage")
    for pr in linked:
        add(pr, "linked_to_issue", True)

    timeline = _nodes(issue["timelineItems"], _CONNECTION_LIMIT, "hasPreviousPage")
    manual_links = {}
    for event in timeline:
        kind = event["__typename"]
        if kind == "CrossReferencedEvent":
            source = event["source"]
            if source["__typename"] == "PullRequest":
                closes = event["willCloseTarget"]
                if type(closes) is not bool:
                    raise IncompleteEvidence("github_invalid_cross_reference")
                add(source, "closing_reference" if closes else "cross_reference", closes)
        elif kind in ("ConnectedEvent", "DisconnectedEvent"):
            for pr in (event["source"], event["subject"]):
                if pr["__typename"] == "PullRequest":
                    manual_links[_link(pr["url"], "pull")] = pr if kind == "ConnectedEvent" else None
        else:
            raise IncompleteEvidence("github_unknown_timeline_event")
    for pr in manual_links.values():
        if pr is not None:
            add(pr, "connected_to_issue", True)

    terms = _words(scope).split()
    if terms and terms[0].casefold() in ("a", "an", "the"):
        terms = terms[1:]
    phrases = [str(number)]
    if terms and terms != [str(number)]:
        phrases.append('"' + " ".join(terms[:2]) + '"')
    for phrase in phrases:
        for pr in _search(repo, phrase):
            closes = _closes_issue(pr["body"] or "", repo, number)
            title_has_scope = _has_phrase(pr["title"], scope)
            exact_title = _words(pr["title"]).casefold() == _words(issue["title"]).casefold()
            add(pr, "search_match", closes or exact_title or title_has_scope, searched=True)
    return [
        {**result, "relationships": sorted(result["relationships"])}
        for _, result in sorted(pulls.items())
    ]


def read_candidate(task):
    """Read strong blocking signals and bounded public context for native judgment."""
    try:
        repo, number = _link(task["upstream"], "issues")
        owner, name = repo.split("/")
        data = _api(
            "graphql", "-f", f"query={_ISSUE_QUERY}",
            "-f", f"owner={owner}", "-f", f"repo={name}", "-F", f"number={number}",
        )["data"]["repository"]
        if data["isPrivate"] is not False:
            raise IncompleteEvidence("github_nonpublic_repository")
        issue = data["issue"]
        if data["nameWithOwner"].lower() != repo or _link(issue["url"], "issues") != (repo, number):
            raise IncompleteEvidence("github_issue_identity_mismatch")
        state, title, body = issue["state"], issue["title"], issue["bodyText"]
        if state not in ("OPEN", "CLOSED"):
            raise IncompleteEvidence("github_unknown_issue_state")
        assignees = sorted(node["login"] for node in _nodes(
            issue["assignees"], _CONNECTION_LIMIT, "hasNextPage",
        ))
        comments = _nodes(issue["comments"], _COMMENT_LIMIT)
        scope = _scope(task, title, body)
        pulls = _open_pulls(issue, repo, number, scope)
        reasons = [
            reason for value, reason in (
                (state != "OPEN", "issue_closed"),
                (assignees, "issue_assigned"),
                (any(pr["competing"] for pr in pulls), "competing_open_pr"),
            )
            if value
        ]
        canonical = {
            "state": state, "id": issue["id"], "updated_at": issue["updatedAt"],
            "title": title, "body": body, "scope": scope, "assignees": assignees,
            "comments": comments, "comments_total": issue["comments"]["totalCount"], "pulls": pulls,
        }
        revision = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()
        context = {
            "issue": issue["url"], "state": state.lower(), "title": title,
            "scope": scope, "excerpt": _excerpt(body, scope), "assignees": assignees,
            "comments_total": issue["comments"]["totalCount"],
            "recent_comments": [
                {"url": comment["url"], "author": (comment["author"] or {}).get("login"),
                 "updated_at": comment["updatedAt"], "excerpt": _excerpt(comment["bodyText"])}
                for comment in comments
            ],
            "open_prs_total": len(pulls),
            "open_prs": [{key: value for key, value in pr.items() if key != "body_digest"} for pr in pulls[:10]],
            "requires_scope_review": bool(comments or pulls),
            "completeness": "verified",
        }
        return {"blocked": bool(reasons), "revision": revision, "reasons": reasons, "context": context}
    except IncompleteEvidence as exc:
        reason = str(exc)
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError):
        reason = "github_invalid_response"
    return {"blocked": True, "revision": None, "reasons": [reason], "context": {"completeness": "unknown"}}
