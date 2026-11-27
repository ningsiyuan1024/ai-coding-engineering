from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from factory import github_events as gh
from factory.state import Store


PR = "https://github.com/example/project/pull/42"
TIME = "2026-11-27T01:00:00Z"
HEAD = "current-head"


def page(nodes=(), cursor=None):
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def snapshot(**updates):
    return {
        "id": "PR-42", "state": "OPEN", "headRefOid": HEAD, "updatedAt": TIME,
        "isDraft": False, "reviewDecision": None, "author": {"login": "author"},
        "lifecycle": {"nodes": []}, "merge": {"nodes": []},
        "reviews": page(), "threads": page(), "statusCheckRollup": None,
    } | updates


def rollup(*checks, cursor=None, oid=HEAD):
    return {"id": "rollup", "commit": {"oid": oid}, "checks": page(checks, cursor)}


def review(identity, verdict, *, head=HEAD, login="reviewer"):
    return {"id": identity, "state": verdict, "commit": {"oid": head}, "author": {"login": login}}


def thread(identity, *, resolved=False, outdated=False, verdict="COMMENTED", login="reviewer"):
    return {
        "id": identity, "isResolved": resolved, "isOutdated": outdated,
        "comments": {"nodes": [{
            "author": {"login": login},
            "pullRequestReview": {"id": identity, "state": verdict},
        }]},
    }


def check(number, name, conclusion, *, status="COMPLETED", app="github-actions"):
    return {
        "__typename": "CheckRun", "id": f"run-{number}", "databaseId": number,
        "name": name, "status": status, "conclusion": conclusion,
        "completedAt": TIME if status == "COMPLETED" else None,
        "checkSuite": {"app": {"slug": app}, "workflowRun": {"workflow": {"id": "workflow"}}},
    }


def timeline(kind, identity, *, at=TIME):
    return {"nodes": [{"__typename": kind, "id": identity, "createdAt": at}]}


class SourceTests(unittest.TestCase):
    task = {"pr": PR, "state": "waiting_upstream", "updated": "2026-11-26T00:00:00+00:00"}

    def events(self, document, *, task=None):
        return gh.read_events(task or self.task, lambda *args, **kwargs: deepcopy(document))

    def test_uses_current_reviews_and_ignores_resolved_outdated_and_self_feedback(self):
        document = snapshot(
            reviews=page([
                review("formal", "CHANGES_REQUESTED"), review("stale", "CHANGES_REQUESTED", head="old"),
                review("dismissed", "DISMISSED"), review("approval", "APPROVED"),
            ]),
            threads=page([
                thread("formal"), thread("inline"), thread("resolved", resolved=True),
                thread("outdated", outdated=True), thread("self", login="Author"),
                thread("pending", verdict="PENDING"), thread("dismissed", verdict="DISMISSED"),
            ]),
        )
        self.assertEqual(self.events(document), [
            gh.Event("review", "github:review:formal"), gh.Event("review", "github:review:inline"),
        ])

    def test_ci_uses_latest_attempt_and_keeps_providers_distinct(self):
        legacy = [
            {"__typename": "StatusContext", "id": "error", "context": "legacy",
             "state": "FAILURE", "createdAt": "2026-11-27T00:00:00Z"},
            {"__typename": "StatusContext", "id": "fixed", "context": "legacy",
             "state": "SUCCESS", "createdAt": TIME},
        ]
        document = snapshot(statusCheckRollup=rollup(
            check(1, "unit", "FAILURE"), check(2, "unit", "SUCCESS"),
            check(3, "lint", "FAILURE"), check(4, "lint", None, status="QUEUED"),
            check(5, "build", "FAILURE"), check(6, "approval", "ACTION_REQUIRED"),
            check(7, "unit", "TIMED_OUT", app="another-app"), *legacy,
        ))
        self.assertEqual(self.events(document), [
            gh.Event("ci", f"github:ci:run-5:{TIME}"),
            gh.Event("ci", f"github:ci:run-7:{TIME}"),
        ])
        with self.assertRaisesRegex(gh.SourceError, "does not match"):
            self.events(snapshot(statusCheckRollup=rollup(check(1, "unit", "FAILURE"), oid="old")))

    def test_pagination_considers_reruns_on_later_pages(self):
        first = snapshot(statusCheckRollup=rollup(check(1, "unit", "FAILURE"), cursor="more"))
        second = snapshot(statusCheckRollup=rollup(check(2, "unit", "SUCCESS")))
        pages = iter((first, second))
        cursors = []

        def query(pr, fields, *, paginated=False, cursor=None):
            cursors.append(cursor)
            return next(pages)

        self.assertEqual(gh.read_events(self.task, query), [])
        self.assertEqual(cursors, [None, "more"])

    def test_ci_ignores_failures_that_predate_tracking_on_the_same_head(self):
        document = snapshot(statusCheckRollup=rollup(check(1, "unit", "FAILURE")))
        task = self.task | {"pr_tracked_at": "2026-11-27T02:00:00Z"}
        self.assertEqual(self.events(document, task=task), [])
        task["pr_tracked_at"] = "2026-11-27T01:00:00.122334Z"
        self.assertEqual(self.events(document, task=task), [gh.Event("ci", f"github:ci:run-1:{TIME}")])

    def test_changed_head_during_pagination_invalidates_entire_read(self):
        pages = iter((
            snapshot(threads=page([thread("first")], cursor="more")),
            snapshot(headRefOid="new", threads=page()),
        ))
        with self.assertRaisesRegex(gh.SourceError, "changed during pagination"):
            gh.read_events(self.task, lambda *args, **kwargs: next(pages))

    def test_lifecycle_tracks_live_state_and_ignores_historical_reopen(self):
        close = timeline("ClosedEvent", "close")
        self.assertEqual(self.events(snapshot(state="CLOSED", lifecycle=close, threads=page([thread("old")]))), [
            gh.Event("closed", "github:timeline:close"),
        ])
        self.assertEqual(self.events(snapshot(state="MERGED", lifecycle=close, merge=timeline("MergedEvent", "merge"))), [
            gh.Event("merged", "github:timeline:merge"),
        ])
        opened = snapshot(lifecycle=timeline("ReopenedEvent", "reopen"))
        self.assertEqual(self.events(opened), [gh.Event("reopened", "github:timeline:reopen")])
        updated = self.task | {"updated": "2026-11-27T02:00:00Z"}
        self.assertEqual(self.events(opened, task=updated), [])
        closed = updated | {"state": "closed", "pr_tracked_at": self.task["updated"]}
        self.assertEqual(self.events(opened, task=closed), [
            gh.Event("reopened", "github:timeline:reopen"),
        ])

    def test_draft_open_pr_does_not_dispatch_review_or_ci(self):
        document = snapshot(
            isDraft=True,
            threads=page([thread("first")]),
            statusCheckRollup=rollup(check(1, "test", "FAILURE")),
        )
        self.assertEqual(self.events(document), [])

    @patch("factory.github_events.subprocess.run")
    def test_cli_transport_uses_only_a_github_query(self, command):
        command.return_value.returncode = 0
        command.return_value.stdout = '{"data":{"repository":{"pullRequest":{"id":"PR"}}}}'
        self.assertEqual(gh._query(PR, "id"), {"id": "PR"})
        args = command.call_args.args[0]
        self.assertEqual(args[:5], ["gh", "api", "graphql", "--hostname", "github.com"])
        self.assertTrue(any(arg.startswith("query=") and "query(" in arg for arg in args))
        self.assertIn("number=42", args)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = Store(Path(scratch.name) / "store.db")
        self.task = self.track(42, "lauren")

    def track(self, number, line, *, with_pr=True):
        task = str(uuid4())
        self.store.discover(task, f"NIN-{number}", f"https://github.com/example/project/issues/{number}", "task")
        self.store.claim(task, line, "version", self.store.path.parent / str(number), "base")
        if with_pr:
            self.store.park(task, line, pr=f"https://github.com/example/project/pull/{number}")
        return task

    def poll(self, events, *, dry_run=False):
        return gh.poll_once(self.store, source=lambda task: events, dry_run=dry_run)

    def test_durable_retries_preserve_owner_and_do_not_wake_twice(self):
        event = gh.Event("ci", "github:ci:new")
        self.poll([event], dry_run=True)
        self.assertEqual(self.store.get(self.task)["state"], "waiting_upstream")
        self.poll([event])
        live = self.store.get(self.task)
        self.assertEqual((live["state"], live["owner"]), ("ready_to_resume", "lauren"))
        run = self.store.begin(self.task, "lauren", "model", "effort")
        self.store.finish(self.task, run, "waiting_upstream")
        self.store = Store(self.store.path)
        self.poll([event])
        self.assertEqual(self.store.get(self.task)["state"], "waiting_upstream")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE key=?", (event.key,)).fetchone()[0], 1)

    def test_terminal_events_and_reopen_keep_original_owner(self):
        for kind, expected in (
            ("closed", "closed"), ("reopened", "ready_to_resume"), ("merged", "closed"),
        ):
            self.poll([gh.Event(kind, f"github:timeline:{kind}")])
            live = self.store.get(self.task)
            self.assertEqual((live["state"], live["owner"]), (expected, "lauren"))
        self.poll([gh.Event("reopened", "github:timeline:reopened")])
        self.assertEqual(self.store.get(self.task)["state"], "closed")

    def test_running_feedback_uses_the_original_handoff_time(self):
        with patch("factory.state.now", return_value="2026-11-27T00:00:00.000000+00:00"):
            task_id = self.track(43, "matt")
            self.store.feedback("https://github.com/example/project/pull/43", "earlier-review", "review")
        with patch("factory.state.now", return_value="2026-11-27T02:00:00.000000+00:00"):
            run = self.store.begin(task_id, "matt", "model", "effort")
        live = snapshot(statusCheckRollup=rollup(check(1, "unit", "FAILURE")))

        def source(task):
            if task["id"] != task_id:
                return []
            self.assertEqual(task["pr_tracked_at"], "2026-11-27T00:00:00.000000+00:00")
            return gh.read_events(task, lambda *args, **kwargs: live)

        gh.poll_once(self.store, source=source)
        self.store.finish(task_id, run, "waiting_upstream")
        self.assertEqual(self.store.get(task_id)["state"], "ready_to_resume")

    def test_partial_source_failure_does_not_stop_other_tracked_prs(self):
        second = self.track(43, "matt")
        self.track(44, "addy", with_pr=False)

        def source(task):
            if task["id"] == self.task:
                raise gh.SourceError("unavailable")
            self.assertEqual(task["id"], second)
            return [gh.Event("review", "github:review:new")]

        report = gh.poll_once(self.store, source=source)
        self.assertEqual(report["errors"], [{"pr": PR, "error": "unavailable"}])
        self.assertEqual(self.store.get(self.task)["state"], "waiting_upstream")
        self.assertEqual(self.store.get(second)["state"], "ready_to_resume")
