import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from factory import linear


def page(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def issue(n=1):
    return {
        "id": f"00000000-0000-4000-8000-{n:012d}", "identifier": f"EX-{n}", "title": "Example",
        "state": {"name": "Todo"}, "project": {"slugId": "project", "id": "project-id"},
        "url": f"https://linear.app/example/issue/EX-{n}",
        "description": "Related [patch](https://github.com/org/repo/pull/7)",
        "attachments": page([{"url": "https://github.com/org/repo/issues/4", "title": "上游 issue"}]),
    }


def listed(*pages, **kwargs):
    with patch.object(linear, "_post", side_effect=pages) as post:
        rows = linear.list_issues("project", ["Todo"], api_key="test-only", **kwargs)
    return rows, post


class IntakeTest(unittest.TestCase):
    def test_snapshot_and_attachment_pagination_and_deduplication(self):
        first = issue()
        first["attachments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "attachment-cursor"}
        a = {"url": "https://github.com/org/repo/pull/8", "title": "Related"}
        rows, post = listed({"issues": page([first], "issue-cursor")},
                            {"issues": {"nodes": [{"attachments": page([a])}]}},
                            {"issues": page([issue(2), issue(2)])}, page_size=1)
        self.assertEqual([r["identifier"] for r in rows], ["EX-1", "EX-2"])
        self.assertEqual(rows[0]["upstream_urls"], ["https://github.com/org/repo/pull/7",
                         "https://github.com/org/repo/issues/4", a["url"]])
        self.assertEqual(linear.discovery_payload(rows[0]), {
            "task_id": first["id"], "identifier": "EX-1", "title": "Example",
            "upstream": "https://github.com/org/repo/issues/4"})
        scope = {"project": {"slugId": {"eq": "project"}}, "state": {"name": {"in": ["Todo"]}}}
        self.assertEqual(post.call_args_list[1].args[2], {
            "after": "attachment-cursor", "filter": {**scope, "id": {"eq": first["id"]}}})
        self.assertEqual(post.call_args_list[-1].args[2]["filter"], scope)
        self.assertEqual(post.call_args_list[-1].args[2]["first"], 1)
        self.assertIsNone(post.call_args_list[0].args[2]["after"])
        self.assertEqual(post.call_args_list[-1].args[2]["after"], "issue-cursor")

    def test_explicit_description_marker_and_ambiguous_fallback(self):
        src = issue()
        src["attachments"] = page([])
        src["description"] += "\n上游 [https://github.com/Org/repo/issues/2#discussion]"
        rows, _ = listed({"issues": page([src])})
        self.assertEqual(linear.discovery_payload(rows[0])["upstream"], "https://github.com/Org/repo/issues/2")
        rows[0]["description"] = "Two equally plausible targets"
        with self.assertRaises(linear.LinearError):
            linear.discovery_payload(rows[0])

    def test_cursor_cycle_and_incomplete_page_do_not_return_partial_results(self):
        p = {"issues": page([issue()], "same")}
        for second in (p, {"issues": {"nodes": []}}, {"issues": page([], "")}):
            with self.subTest(second=second), self.assertRaises(linear.LinearError):
                listed(p, second)

    def test_empty_scope_is_rejected_without_request(self):
        with patch.object(linear, "_post") as post:
            for slug, states in [("", ["Todo"]), ("project", []), ("project", ["  "]), ("project", "Todo")]:
                with self.assertRaises(ValueError):
                    linear.list_issues(slug, states)
            post.assert_not_called()

    def test_record_must_still_match_scope(self):
        for field, value in [("state", {"name": "Canceled"}), ("project", {"slugId": "other"}),
                             ("id", "EX-1"), ("identifier", "EX-3")]:
            with self.subTest(field=field), self.assertRaises(linear.LinearError):
                listed({"issues": page([{**issue(), field: value}])}, identifier="EX-1")
        rows, post = listed({"issues": page([])}, issue_id=issue()["id"], identifier="EX-1")
        self.assertEqual(rows, [])
        filters = post.call_args.args[2]["filter"]
        self.assertEqual(filters["number"], {"eq": 1})
        self.assertEqual(filters["team"], {"key": {"eq": "EX"}})
        self.assertEqual(filters["id"], {"eq": issue()["id"]})

    def test_absent_key_and_control_characters(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(linear, "_post") as post:
            for key in (None, "injected\r\nfield"):
                with self.assertRaises(linear.LinearAuthError):
                    linear.list_issues("project", ["Todo"], api_key=key)
            post.assert_not_called()

    def test_transport_http_graphql_errors_and_safe_messages(self):
        cases = [(401, {}, linear.LinearAuthError), (403, {}, linear.LinearAuthError),
                 (429, {}, linear.LinearRateLimited), (400, {"code": "RATELIMITED"}, linear.LinearRateLimited),
                 (200, {"code": "UNAUTHENTICATED"}, linear.LinearAuthError),
                 (200, {"code": "UNKNOWN"}, linear.LinearError)]
        for status, extensions, error_type in cases:
            with self.subTest(status=status, ext=extensions), patch("http.client.HTTPSConnection") as connect:
                response = connect.return_value.getresponse.return_value
                response.status = status
                response.getheader.return_value = "7"
                response.read.return_value = json.dumps({
                    "data": {"issues": page([])}, "errors": [{"message": "private-body", "extensions": extensions}]})
                with self.assertRaises(error_type) as error:
                    linear._post("private-key", linear.ISSUES, {})
                self.assertNotIn("private", str(error.exception))
                if error_type == linear.LinearRateLimited:
                    self.assertEqual(error.exception.retry_after, 7)
                connect.return_value.close.assert_called_once()

    def test_cli_interrupt_and_errors_have_distinct_exits_and_no_partial_json(self):
        args = ["--project-slug", "project", "--state", "Todo"]
        for exc, code in [(KeyboardInterrupt(), 130), (linear.LinearRateLimited(), 75),
                          (linear.LinearAuthError("unauthorized"), 77), (linear.LinearError("bad page"), 1)]:
            stdout = io.StringIO()
            with patch.object(linear, "list_issues", side_effect=exc), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                self.assertEqual(linear.main(args), code)
            self.assertEqual(stdout.getvalue(), "")

    def test_handoff_through_real_coordinator_is_idempotent(self):
        rows, _ = listed({"issues": page([issue()])})
        root = Path(linear.__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp).resolve() / "factory.db")
            argv = ["--project-slug", "project", "--state", "Todo", "--identifier", "EX-1", "--factory-state", db]
            with patch.object(linear, "list_issues", return_value=rows), redirect_stdout(io.StringIO()):
                for _ in range(2):
                    self.assertEqual(linear.main(argv), 0)
            pool = json.loads(subprocess.check_output(
                [sys.executable, "-m", "factory", "--state", db, "pool"], cwd=root))
            self.assertEqual(len(pool), 1)
            self.assertEqual(pool[0]["id"], rows[0]["id"])
            self.assertEqual(pool[0]["upstream"], "https://github.com/org/repo/issues/4")
            self.assertIsNone(pool[0]["owner"])

    def test_handoff_after_scope_exit_does_not_create_db(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp).resolve() / "factory.db"
            with patch.object(linear, "list_issues", return_value=[]), redirect_stderr(io.StringIO()):
                code = linear.main(["--project-slug", "project", "--state", "Todo",
                                    "--identifier", "EX-1", "--factory-state", str(db)])
            self.assertNotEqual(code, 0)
            self.assertFalse(db.exists())

    def test_native_intake_excludes_scope_changes_and_skips_unchanged_import(self):
        rows, _ = listed({"issues": page([issue(), issue(2)])})
        with TemporaryDirectory() as tmp:
            db = Path(tmp).resolve() / "factory.db"
            selected = {issue()["id"], issue(3)["id"]}
            with patch.object(linear, "_native_snapshot", return_value=("project", ["Todo"], selected)), \
                 patch.object(linear, "list_issues", return_value=rows), \
                 patch.object(linear, "_import_pool", wraps=linear._import_pool) as importer:
                wanted = {"matched": 1, "eligible_ids": [issue()["id"]]}
                self.assertEqual(linear.sync_symphony(tmp, db), {**wanted, "imported": 1, "unchanged": 0})
                self.assertEqual(linear.sync_symphony(tmp, db), {**wanted, "imported": 0, "unchanged": 1})
                importer.assert_called_once()
                self.assertEqual(importer.call_args.args[0][0]["id"], issue()["id"])

    def test_empty_native_queue_does_not_reread_or_open_database(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp).resolve() / "factory.db"
            with patch.object(linear, "_native_snapshot", return_value=("project", ["Backlog"], set())), \
                 patch.object(linear, "list_issues") as read:
                self.assertEqual(linear.sync_symphony(tmp, db)["matched"], 0)
                read.assert_not_called()
                self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
