"""Read-only Linear intake. Use the UUID as the claim key, the identifier for display."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID


ISSUES = """
query FactoryIssues($filter: IssueFilter!, $first: Int!, $after: String) {
  issues(filter: $filter, first: $first, after: $after, includeArchived: false) {
    nodes {
      id identifier title description url state { name } project { id slugId }
      attachments(first: 10) { nodes { title url } pageInfo { hasNextPage endCursor } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
ATTACHMENTS = """
query FactoryAttachments($filter: IssueFilter!, $after: String!) {
  issues(filter: $filter, first: 1, includeArchived: false) {
    nodes { attachments(first: 50, after: $after) {
      nodes { title url } pageInfo { hasNextPage endCursor }
    } }
  }
}
"""
UPSTREAM = re.compile(
    r"https://(?:github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+"
    r"|gitlab\.com/(?:[\w.-]+/)+-/(?:issues|merge_requests)/\d+)"
    r"(?![\w.-])(?:#[\w.-]+)?", re.I
)
SOURCE_LABEL = re.compile(r"^(?:上游|upstream)(?:\s*(?:issue|pr|pull request|任务|链接))?(?:\s*[·:：]|\s*$)", re.I)


class LinearError(RuntimeError):
    """Messages may be logged. Never include API bodies, headers or issue text."""


class LinearAuthError(LinearError):
    pass


class LinearRateLimited(LinearError):
    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        delay = f" after {retry_after}s" if retry_after is not None else " later"
        super().__init__(f"Linear rate limit reached. Retry{delay}.")


def _post(key, query, variables):
    connection = http.client.HTTPSConnection("api.linear.app", timeout=30)
    try:
        connection.request("POST", "/graphql", json.dumps({"query": query, "variables": variables}),
                           {"Content-Type": "application/json", "Authorization": key})
        response = connection.getresponse()
        status = response.status
        if status in (401, 403):
            raise LinearAuthError("Linear denied access. Check the credential and project access.")
        retry = response.getheader("Retry-After") or ""
        delay = int(retry) if retry.isascii() and retry.isdigit() and len(retry) <= 10 else None
        if status == 429:
            raise LinearRateLimited(delay)
        body = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise LinearError("Linear connection failed.") from exc
    finally:
        connection.close()

    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise LinearError(f"Invalid Linear response (HTTP {status}).") from None
    if not isinstance(result, dict):
        raise LinearError("Invalid Linear response.")
    errors = result.get("errors")
    if errors:
        codes = {str((e.get("extensions") or {}).get("code", "")).upper()
                 for e in errors if isinstance(e, dict) and isinstance(e.get("extensions"), dict)}
        if codes & {"RATELIMITED", "RATE_LIMITED"}:
            raise LinearRateLimited(delay)
        if codes & {"UNAUTHENTICATED", "AUTHENTICATION_ERROR", "FORBIDDEN"}:
            raise LinearAuthError("Linear denied access. Check the credential and project access.")
        raise LinearError(f"Linear rejected the query (HTTP {status}).")
    if status != 200 or not isinstance(result.get("data"), dict):
        raise LinearError(f"Linear query failed (HTTP {status}).")
    return result["data"]


def _nodes(connection):
    nodes = connection["nodes"]
    if not isinstance(nodes, list):
        raise LinearError("Malformed Linear nodes.")
    return nodes


def _cursor(connection, seen):
    info = connection["pageInfo"]
    if info["hasNextPage"] is False:
        return None
    value = info["endCursor"]
    if info["hasNextPage"] is not True or not isinstance(value, str) or not value or value in seen:
        raise LinearError("Invalid or repeated Linear page cursor.")
    seen.add(value)
    return value


def _attachments(key, node, scope):
    page = node["attachments"]
    seen = set()
    attachments = []
    while True:
        for item in _nodes(page):
            attachments.append({"url": _text(item, "url"), "title": _text(item, "title")})
        after = _cursor(page, seen)
        if after is None:
            return attachments
        filters = {**scope, "id": {"eq": node["id"]}}
        result = _post(key, ATTACHMENTS, {"filter": filters, "after": after})
        matches = _nodes(result["issues"])
        if len(matches) != 1:
            raise LinearError("Issue left the selected scope while reading attachments. Poll again.")
        page = matches[0]["attachments"]


def _text(record, name):
    value = record[name]
    if not isinstance(value, str) or not value.strip():
        raise LinearError(f"Missing Linear field {name}.")
    return value


def list_issues(project_slug, states, *, api_key=None, page_size=50, issue_id=None, identifier=None):
    """Fetch a complete snapshot or raise. Pass the Linear project's slugId and state names.

    Optional issue_id narrows a Symphony handoff to one UUID. No remote mutations.
    A rate limit raises LinearRateLimited with retry_after in seconds if supplied.
    """
    if not isinstance(project_slug, str) or not project_slug.strip():
        raise ValueError("A Linear project slug is required.")
    if isinstance(states, str) or not states:
        raise ValueError("Provide at least one state name as a sequence.")
    names = list(dict.fromkeys(states))
    if not names or not all(isinstance(s, str) and s.strip() for s in names):
        raise ValueError("Empty state name.")
    if not 1 <= page_size <= 100:
        raise ValueError("Page size must be 1 through 100.")
    scope = {"project": {"slugId": {"eq": project_slug}}, "state": {"name": {"in": names}}}
    if issue_id is not None:
        issue_id = str(UUID(issue_id))
        scope["id"] = {"eq": issue_id}
    if identifier is not None:
        team, separator, number = identifier.rpartition("-")
        if not separator or not team or not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("Expected a Linear issue identifier.")
        scope.update(team={"key": {"eq": team}}, number={"eq": int(number)})
    key = api_key or os.getenv("OSS_LINEAR_API_KEY") or os.getenv("LINEAR_API_KEY")
    if not key or not key.strip() or not key.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise LinearAuthError("Set OSS_LINEAR_API_KEY or LINEAR_API_KEY in the calling process.")
    cursor = None
    seen = set()
    issues = {}
    try:
        while True:
            variables = {"filter": scope, "first": page_size, "after": cursor}
            page = _post(key, ISSUES, variables)["issues"]
            for node in _nodes(page):
                uid = str(UUID(_text(node, "id")))
                state = _text(node["state"], "name")
                display_id = _text(node, "identifier")
                if (node["project"]["slugId"] != project_slug or state not in names
                        or (issue_id and uid != issue_id) or (identifier and display_id != identifier)):
                    raise LinearError("Linear returned an issue outside the requested scope.")
                description = node["description"] or ""
                attachments = _attachments(key, node, scope)
                links = UPSTREAM.findall(description)
                for item in attachments:
                    links.extend(UPSTREAM.findall(item["url"]))
                issues[uid] = {
                    "id": uid, "identifier": display_id, "title": _text(node, "title"),
                    "state": state, "url": _text(node, "url"), "project_id": _text(node["project"], "id"),
                    "project_slug": project_slug, "description": description, "attachments": attachments,
                    "upstream_urls": list(dict.fromkeys(links)),
                }
            cursor = _cursor(page, seen)
            if cursor is None:
                return list(issues.values())
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LinearError("Malformed Linear issue response.") from None


def discovery_payload(issue):
    """Return kwargs for Store.discover; refuse ambiguous or unsupported upstream identities."""
    marked = [a["url"] for a in issue["attachments"] if SOURCE_LABEL.match(a["title"].strip())]
    for line in issue["description"].splitlines():
        if re.match(r"^\s*(?:上游|upstream)\s*(?::|：|\[|<|https://)", line, re.I):
            marked.extend(UPSTREAM.findall(line))
    candidates = marked or issue["upstream_urls"]
    canonical = {}
    for url in candidates:
        base = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/[1-9]\d*", base):
            if marked:
                raise LinearError("Marked upstream target is not a canonical GitHub issue or PR.")
            continue
        canonical[base.lower()] = base
    if len(canonical) != 1:
        raise LinearError("Select one upstream GitHub issue or PR before discovery.")
    return {"task_id": issue["id"], "identifier": issue["identifier"],
            "upstream": next(iter(canonical.values())), "title": issue["title"]}


def _import_changed(issues, state):
    """Compare identity metadata only. Native ownership and lifecycle belong to the coordinator."""
    from .state import Store

    rows = []
    for issue in issues:
        payload = discovery_payload(issue)
        rows.append({"id": payload["task_id"], "identifier": payload["identifier"],
                     "upstream": payload["upstream"], "title": payload["title"]})
    if not rows:
        return {"matched": 0, "imported": 0, "unchanged": 0}
    previous = {row["id"]: row for row in Store(state).pool()} if state.exists() else {}
    changed = [row for row in rows if row["id"] not in previous or
               any(previous[row["id"]][k] != v for k, v in row.items())]
    if changed:
        _import_pool(changed, state)
    return {"matched": len(rows), "imported": len(changed), "unchanged": len(rows) - len(changed)}


def _import_pool(rows, state):
    with TemporaryDirectory(prefix="factory-linear-") as directory:
        path = Path(directory) / "issue.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        cmd = [sys.executable, "-m", "factory", "--state", str(state), "import-pool", str(path)]
        result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, check=False)
        if result.returncode:
            raise LinearError(f"Factory import-pool failed (exit {result.returncode}).")


def _native_snapshot(root):
    executable = shutil.which("elixir.bat" if os.name == "nt" else "elixir")
    if not executable:
        raise LinearError("Place the installed Elixir and Erlang bin directories on this process's PATH.")
    scripts = Path(__file__).resolve().parents[1] / "workflows" / "symphony"
    # The code and workflow paths are fixed. No tracker data is placed in shell arguments.
    env = {**os.environ, "FACTORY_SYMPHONY_ROOT": root.resolve().as_posix()}
    try:
        result = subprocess.run([executable, "intake.exs"], cwd=scripts, env=env,
                                capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        raise LinearError("Native Symphony tracker timed out.") from None
    if result.returncode == 77:
        raise LinearAuthError("Native Symphony tracker requires an authorized Linear key.")
    if result.returncode == 75:
        raise LinearRateLimited()
    if result.returncode:
        raise LinearError(f"Native Symphony tracker exited {result.returncode}. Check the compiled checkout and workflow.")
    try:
        data = json.loads(result.stdout)
        ids = {str(UUID(value)) for value in data["ids"]}
        states = data["states"]
        slug = data["project_slug"]
        if not isinstance(states, list) or not states or not isinstance(slug, str) or not slug:
            raise ValueError()
        return slug, states, ids
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LinearError("Invalid native Symphony tracker snapshot.") from None


def sync_symphony(symphony_root, state):
    """Reconcile once. eligible_ids is the current poll's scope, including unchanged imports."""
    state = Path(state)
    if not state.is_absolute():
        raise ValueError("Use an absolute coordinator database path.")
    slug, states, ids = _native_snapshot(Path(symphony_root))
    # The native candidate struct lacks attachments. Read full metadata with fresh scope
    # checks and discard issues that entered or exited between the two reads.
    scoped = list_issues(slug, states) if ids else []
    eligible = [row for row in scoped if row["id"] in ids]
    return {**_import_changed(eligible, state), "eligible_ids": [row["id"] for row in eligible]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symphony-root", type=Path, help="Poll the installed Symphony tracker using the factory workflow")
    parser.add_argument("--project-slug", help="Linear slugId as used by Symphony")
    parser.add_argument("--state", action="append")
    parser.add_argument("--issue-id")
    parser.add_argument("--identifier")
    parser.add_argument("--factory-state", type=Path, help="Absolute path to the factory coordinator database")
    parser.add_argument("--page-size", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.symphony_root is not None:
            if not args.factory_state or any((args.project_slug, args.state, args.issue_id, args.identifier)):
                raise ValueError("Use --symphony-root with --factory-state. The workflow supplies the intake scope.")
            print(json.dumps(sync_symphony(args.symphony_root, args.factory_state)))
            return 0
        if args.factory_state is not None:
            if not args.factory_state.is_absolute() or not (args.identifier or args.issue_id):
                raise ValueError("--factory-state requires an absolute path and --identifier or --issue-id.")
        rows = list_issues(args.project_slug, args.state, page_size=args.page_size,
                           issue_id=args.issue_id, identifier=args.identifier)
        if args.factory_state is not None:
            if len(rows) != 1:
                raise LinearError("Handoff requires exactly one issue still in the selected project and state.")
            rows = _import_changed(rows, args.factory_state)
        print(json.dumps(rows, ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError:
        print("Could not complete local Linear handoff or write output.", file=sys.stderr)
        return 1
    except (LinearError, ValueError) as exc:
        print(exc, file=sys.stderr)
        if isinstance(exc, LinearRateLimited):
            return 75
        if isinstance(exc, LinearAuthError):
            return 77
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
