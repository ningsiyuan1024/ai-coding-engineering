"""Poll tracked GitHub PRs once and dispatch actionable events to their owners."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess

from .state import Store, upstream_key


_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_CONNECTIONS = {
    "reviews": f"""
        reviews: latestOpinionatedReviews(first: 100, after: $cursor) {{
          {_PAGE_INFO}
          nodes {{ id state author {{ login }} commit {{ oid }} }}
        }}
    """,
    "threads": f"""
        threads: reviewThreads(first: 100, after: $cursor) {{
          {_PAGE_INFO}
          nodes {{
            id isOutdated isResolved
            comments(first: 1) {{
              nodes {{ author {{ login }} pullRequestReview {{ id state }} }}
            }}
          }}
        }}
    """,
    "checks": f"""
        statusCheckRollup {{
          id
          commit {{ oid }}
          checks: contexts(first: 100, after: $cursor) {{
            {_PAGE_INFO}
            nodes {{
              __typename
              ... on CheckRun {{
                id databaseId name status conclusion completedAt
                checkSuite {{ app {{ slug }} workflowRun {{ workflow {{ id }} }} }}
              }}
              ... on StatusContext {{ id context state createdAt }}
            }}
          }}
        }}
    """,
}
_TIMELINE = """
    lifecycle: timelineItems(last: 1, itemTypes: [CLOSED_EVENT, REOPENED_EVENT, MERGED_EVENT]) {
      nodes {
        __typename
        ... on ClosedEvent { id createdAt }
        ... on ReopenedEvent { id createdAt }
        ... on MergedEvent { id createdAt }
      }
    }
    merge: timelineItems(last: 1, itemTypes: [MERGED_EVENT]) {
      nodes { ... on MergedEvent { id } }
    }
"""
_META = "id state headRefOid updatedAt isDraft reviewDecision author { login }"
_FAILED_CHECKS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}
_FAILED_STATUSES = {"ERROR", "FAILURE"}


class SourceError(RuntimeError):
    """A GitHub read could not produce a consistent and complete snapshot."""


@dataclass(frozen=True, order=True)
class Event:
    kind: str
    key: str


def _query(pr, fields, *, paginated=False, cursor=None):
    owner, repo, number = upstream_key(pr).split("/")
    if "/pull/" not in pr or not all(re.fullmatch(r"[a-z0-9_.-]+", part) for part in (owner, repo)):
        raise ValueError("Expected a canonical GitHub pull request URL")
    declaration = ", $cursor: String" if paginated else ""
    query = f"""
        query($owner: String!, $repo: String!, $number: Int!{declaration}) {{
          repository(owner: $owner, name: $repo) {{
            pullRequest(number: $number) {{ {fields} }}
          }}
        }}
    """
    command = [
        "gh", "api", "graphql", "--hostname", "github.com",
        "--raw-field", f"query={query}", "--raw-field", f"owner={owner}",
        "--raw-field", f"repo={repo}", "--field", f"number={number}",
    ]
    if cursor is not None:
        command.extend(("--raw-field", f"cursor={cursor}"))
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
            timeout=45, check=False,
        )
    except FileNotFoundError as error:
        raise SourceError("GitHub CLI (gh) is unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise SourceError("GitHub query timed out") from error
    if result.returncode:
        raise SourceError(f"GitHub CLI exited with status {result.returncode}; check authentication and API access")
    data = json.loads(result.stdout)
    if data.get("errors"):
        raise SourceError("GitHub returned GraphQL errors")
    repository = data["data"]["repository"]
    if repository is None or repository["pullRequest"] is None:
        raise SourceError("GitHub pull request is missing or inaccessible")
    return repository["pullRequest"]


def _connection(document, name):
    if name != "checks":
        return document[name]
    rollup = document["statusCheckRollup"]
    if rollup is None:
        return None
    if rollup["commit"]["oid"] != document["headRefOid"]:
        raise SourceError("Check rollup does not match the current PR head")
    return rollup["checks"]


def _read_connection(pr, name, initial, query):
    original_rollup = initial.get("statusCheckRollup")
    document = initial
    nodes = []
    cursors = set()
    while (connection := _connection(document, name)) is not None:
        nodes.extend(node for node in connection["nodes"] if node is not None)
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
        if not cursor or cursor in cursors:
            raise SourceError("GitHub pagination did not advance")
        cursors.add(cursor)
        document = query(pr, _META + _CONNECTIONS[name], paginated=True, cursor=cursor)
        keys = ("id", "headRefOid", "state", "updatedAt", "isDraft", "reviewDecision")
        if any(document[key] != initial[key] for key in keys):
            raise SourceError("PR changed during pagination; retry the poll")
        if name == "checks":
            rollup = document["statusCheckRollup"]
            if rollup is None or rollup["id"] != original_rollup["id"]:
                raise SourceError("Check rollup changed during pagination; retry the poll")
    return nodes


def _login(actor):
    return actor["login"].casefold() if actor else None


def _review_events(snapshot, reviews, threads):
    author = _login(snapshot["author"])
    ids = set()
    for review in reviews:
        reviewer = _login(review["author"])
        if (review["state"] == "CHANGES_REQUESTED" and reviewer and reviewer != author
                and review["commit"] and review["commit"]["oid"] == snapshot["headRefOid"]):
            ids.add(review["id"])
    for thread in threads:
        if thread["isResolved"] or thread["isOutdated"]:
            continue
        comments = thread["comments"]["nodes"]
        if not comments or comments[0] is None:
            continue
        comment = comments[0]
        reviewer = _login(comment["author"])
        review = comment["pullRequestReview"]
        if reviewer and reviewer != author and (review is None or review["state"] not in ("PENDING", "DISMISSED")):
            ids.add(review["id"] if review is not None else thread["id"])
    return {Event("review", f"github:review:{identity}") for identity in ids}


def _check_events(checks, tracked_since):
    latest = {}
    for check in checks:
        if check["__typename"] == "CheckRun":
            suite = check["checkSuite"] or {}
            app = (suite.get("app") or {}).get("slug")
            workflow = ((suite.get("workflowRun") or {}).get("workflow") or {}).get("id")
            context = ("run", app, workflow, check["name"])
            rank = int(check["databaseId"])
            failed = check["status"] == "COMPLETED" and check["conclusion"] in _FAILED_CHECKS
            time = check["completedAt"]
        elif check["__typename"] == "StatusContext":
            context = ("status", check["context"])
            failed = check["state"] in _FAILED_STATUSES
            time = check["createdAt"]
            # If the provider returns two statuses for the same second, prefer
            # the non-error result to avoid waking on an ambiguous older error.
            rank = (datetime.fromisoformat(time), not failed, check["id"])
        else:
            continue
        event = None
        if failed and time and datetime.fromisoformat(time) >= tracked_since:
            event = Event("ci", f"github:ci:{check['id']}:{time}")
        prior = latest.get(context)
        if prior is None or rank > prior[0]:
            latest[context] = (rank, event)
        elif rank == prior[0] and event != prior[1]:
            raise SourceError("Check changed during pagination; retry the poll")
    return {event for _, event in latest.values() if event is not None}


def _lifecycle(snapshot, task):
    state = snapshot["state"]
    nodes = snapshot["lifecycle"]["nodes"]
    latest = nodes[0] if nodes else None
    if state == "MERGED":
        merge = snapshot["merge"]["nodes"]
        if not merge or not merge[0]:
            raise SourceError("Merged PR has no visible GitHub merge event")
        return Event("merged", f"github:timeline:{merge[0]['id']}")
    if state == "CLOSED":
        if not latest or latest["__typename"] != "ClosedEvent":
            raise SourceError("Closed PR has no matching GitHub close event")
        return Event("closed", f"github:timeline:{latest['id']}")
    if state != "OPEN":
        raise SourceError("GitHub returned an unrecognized PR state")
    if latest is None:
        return None
    if latest["__typename"] != "ReopenedEvent":
        raise SourceError("Open PR has an inconsistent GitHub lifecycle")
    if datetime.fromisoformat(latest["createdAt"]) >= _tracked_since(task):
        return Event("reopened", f"github:timeline:{latest['id']}")
    return None


def _tracked_since(task):
    # GitHub timestamps are rounded to a second. Retain observations from the
    # handoff second because their relative order cannot be recovered.
    return datetime.fromisoformat(task.get("pr_tracked_at") or task["updated"]).replace(microsecond=0)


def read_events(task, query=_query):
    """Read current actionable events for one PR, ordered for Store.feedback."""
    pr = task["pr"]
    fields = _META + _TIMELINE + "".join(_CONNECTIONS.values())
    snapshot = query(pr, fields, paginated=True)
    lifecycle = _lifecycle(snapshot, task)
    events = [lifecycle] if lifecycle is not None else []
    if snapshot["state"] != "OPEN" or snapshot["isDraft"]:
        return events
    reviews = _read_connection(pr, "reviews", snapshot, query)
    threads = _read_connection(pr, "threads", snapshot, query)
    checks = _read_connection(pr, "checks", snapshot, query)
    feedback = _review_events(snapshot, reviews, threads) | _check_events(checks, _tracked_since(task))
    return events + sorted(feedback)


def _tracking_times(store):
    # Read the coordinator's durable first PR handoff. Older variants of
    # Store.finish had no link flag, so use their first upstream handoff.
    with store.transaction() as db:
        rows = db.execute("""
            SELECT events.task,
                   coalesce(min(CASE WHEN json_extract(events.data, '$.linked_pr') = 1 THEN events.at END),
                            min(CASE WHEN events.type = 'run_finished'
                                     AND json_extract(events.data, '$.outcome') = 'waiting_upstream'
                                     THEN events.at END)) AS tracked_at
              FROM tasks
              JOIN events ON events.task = tasks.id
             WHERE tasks.pr IS NOT NULL AND events.type IN ('parked', 'run_finished')
             GROUP BY events.task
        """)
        return {row["task"]: row["tracked_at"] for row in rows}


def poll_once(store, *, source=read_events, dry_run=False):
    """Continue per PR on source errors and let Store deduplicate event IDs."""
    records = []
    errors = []
    tasks = [task for task in store.pool() if task["pr"]]
    tracked_at = _tracking_times(store) if tasks else {}
    for task in tasks:
        try:
            events = source(task | {"pr_tracked_at": tracked_at.get(task["id"])})
        except (SourceError, OSError, KeyError, TypeError, ValueError) as error:
            errors.append({"pr": task["pr"], "error": str(error)})
            continue
        state = task["state"]
        for event in events:
            if not dry_run:
                state = store.feedback(task["pr"], event.key, event.kind)["state"]
        records.append({
            "pr": task["pr"], "owner": task["owner"], "state": state,
            "events": [{"id": event.key, "kind": event.kind} for event in events],
        })
    return {"dry_run": dry_run, "pull_requests": records, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".factory-local"))
    parser.add_argument("--state", type=Path, help="Coordinator database (default ROOT/factory.db)")
    parser.add_argument("--dry-run", action="store_true", help="Show observations without dispatching")
    args = parser.parse_args(argv)
    path = args.state or args.root / "factory.db"
    if not path.is_file():
        parser.error(f"Coordinator database does not exist: {path}")
    report = poll_once(Store(path), dry_run=args.dry_run)
    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
