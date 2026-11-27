import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from factory import runner
from factory.runtime import NativeQuestion, RunResult
from factory.state import Conflict, Store
from factory.telemetry import LocalTraceReceiver, read_logs, read_metrics, read_trace


UPSTREAM = "https://github.com/owner/repo"
SESSION_ID = "native-session-fixture"


class LocalHost:
    def __init__(self, action):
        self.action = action
        self.turns = []

    def run(self, prompt, model, effort, *, thread_id, on_thread, on_event):
        self.turns.append((thread_id, prompt))
        on_thread(SESSION_ID)
        on_event("runtime.usage", {"usage.inputTokens": 17, "usage.outputTokens": 0})
        self.action()
        return RunResult(SESSION_ID, f"turn-{len(self.turns)}", "private native result")


def values(record):
    return {item["key"]: item["value"] for item in record["attributes"]}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        location = self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd()))
        self.directory = Path(location)
        self.root = self.directory / "factory"
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        runner.git(self.repo, "init", "-q")
        runner.git(self.repo, "remote", "add", "upstream", UPSTREAM + ".git")
        runner.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                   "commit", "--allow-empty", "-qm", "Test repository")
        self.profiles = self.root / "profiles"
        self.store = Store(self.root / "factory.db")
        self.task_id = str(uuid4())
        self.store.discover(self.task_id, "T-1", UPSTREAM + "/issues/1", "Private Linear title")
        self.task = runner.claim(self.store, self.task_id, "sylan", self.repo, "HEAD", self.root, self.profiles)

    def run_with(self, action=lambda: None, endpoint=None, read_only=True):
        runtime = LocalHost(action)
        with patch("factory.runner.host", return_value=nullcontext(runtime)):
            outcome = runner.run(self.store, self.task_id, self.root, self.profiles,
                                 "test-model", "medium", instructions="Private instructions",
                                 endpoint=endpoint, read_only=read_only)
        return outcome, runtime.turns

    def run_records(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY started")]

    def persisted_signals(self, run):
        directory = self.root / "runs" / run["id"]
        receiver = LocalTraceReceiver(directory / "otlp.ndjson")
        trace, = read_trace(receiver.path, run["trace_id"])
        metric, = read_metrics(receiver.metrics_path, run["trace_id"])
        log, = read_logs(receiver.logs_path, run["trace_id"])
        self.assertEqual(log["spanId"], trace["spanId"])
        self.assertEqual(metric["gauge"]["dataPoints"][0]["exemplars"][0]["spanId"], trace["spanId"])
        return directory, trace, metric, log

    def test_two_runs_get_independent_otlp_receipts_and_resume_original_session(self):
        first, first_turns = self.run_with()
        self.assertIsNone(first_turns[0][0])
        decision = self.store.park(self.task_id, "sylan", question="Resume?")
        self.store.decide(decision, "Recorded decision text")
        second, second_turns = self.run_with()
        self.assertEqual(second_turns[0][0], SESSION_ID)
        self.assertIn("Recorded decision text", second_turns[0][1])
        self.assertNotEqual(first["trace_id"], second["trace_id"])
        first_run, second_run = self.run_records()
        for outcome, stored in ((first, first_run), (second, second_run)):
            directory, trace, _, log = self.persisted_signals(stored)
            self.assertEqual(outcome["trace_id"], stored["trace_id"])
            self.assertIsNone(outcome["cost"])
            self.assertEqual(Path(outcome["telemetry"]["traces"]), directory / "otlp.ndjson")
            self.assertEqual(Path(outcome["telemetry"]["metrics"]), directory / "otlp.metrics.ndjson")
            self.assertEqual(Path(outcome["telemetry"]["logs"]), directory / "otlp.logs.ndjson")
            self.assertEqual(Path(outcome["artifact"]).read_text(encoding="utf-8"), "private native result")
            self.assertEqual(values(trace)["factory.cost.status"], {"stringValue": "unknown"})
            self.assertEqual(values(trace)["factory.session.id"], {"stringValue": SESSION_ID})
            self.assertEqual(values(log)["factory.run.status"], {"stringValue": "ok"})
            artifact, = [event for event in trace["events"] if event["name"] == "artifact.link"]
            expected = hashlib.sha256(Path(outcome["artifact"]).read_bytes()).hexdigest()
            self.assertEqual(values(artifact)["factory.artifact.sha256"], {"stringValue": expected})
            for path in outcome["telemetry"].values():
                data = Path(path).read_text(encoding="utf-8")
                for private in ("private native result", "Private instructions", "Recorded decision text",
                                "Private Linear title"):
                    self.assertNotIn(private, data)

    def test_checkout_on_unclaimed_branch_is_rejected_before_work_starts(self):
        worktree = Path(self.task["worktree"])
        runner.git(worktree, "switch", "-c", "unclaimed-branch")
        with patch("factory.runner.host") as host, self.assertRaisesRegex(ValueError, "linked worktree"):
            runner.run(self.store, self.task_id, self.root, self.profiles,
                       "test-model", "medium", instructions="test")
        host.assert_not_called()
        self.assertEqual(self.run_records(), [])
        self.assertEqual(self.store.get(self.task_id)["state"], "claimed")

    def test_native_question_is_discovered_and_answered_in_the_owning_session(self):
        def ask():
            raise NativeQuestion("Which model budget should this task use?")
        paused, _ = self.run_with(ask)
        gate, = self.store.inbox()
        self.assertEqual(gate["id"], paused["decision"])
        self.assertEqual(gate["native_session"], SESSION_ID)
        self.assertIsNone(self.store.get(self.task_id)["active_run"])
        self.store.decide(gate["id"], "Use offline fixtures")
        _, turns = self.run_with()
        self.assertEqual(turns[0][0], SESSION_ID)
        self.assertIn("Use offline fixtures", turns[0][1])

    def test_local_write_turn_links_private_worktree_evidence_to_trace(self):
        def edit():
            (Path(self.task["worktree"]) / "new.txt").write_text("new contribution", encoding="utf-8")
        outcome, _ = self.run_with(edit, read_only=False)
        run, = self.run_records()
        directory, trace, _, _ = self.persisted_signals(run)
        manifest = directory / "worktree-evidence" / "worktree.json"
        self.assertIn("new.txt", manifest.read_text(encoding="utf-8"))
        hashes = [values(event)["factory.artifact.sha256"]["stringValue"]
                  for event in trace["events"] if event["name"] == "artifact.link"]
        self.assertIn(hashlib.sha256(manifest.read_bytes()).hexdigest(), hashes)
        self.assertTrue(Path(outcome["artifact"]).is_file())

    def test_invalid_endpoint_releases_run_without_claiming_otlp_receipt(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            self.run_with(endpoint="https://example.invalid/v1/traces")
        run, = self.run_records()
        self.assertEqual(run["state"], "failed")
        self.assertIsNone(run["trace_id"])
        directory = self.root / "runs" / run["id"]
        self.assertEqual(json.loads((directory / "failure.json").read_bytes()),
                         {"type": "ValueError", "trace_id": None})
        self.assertFalse((directory / "otlp.ndjson").exists())

    def test_host_exception_keeps_original_error_and_failure_receipts(self):
        def fail():
            raise RuntimeError("private original diagnostic")

        with self.assertRaisesRegex(RuntimeError, "private original diagnostic"):
            self.run_with(action=fail)
        run, = self.run_records()
        self.assertEqual(run["state"], "failed")
        directory, trace, _, log = self.persisted_signals(run)
        failure = directory / "failure.json"
        self.assertEqual(json.loads(failure.read_bytes()), {"type": "RuntimeError", "trace_id": run["trace_id"]})
        self.assertEqual(values(log)["factory.run.status"], {"stringValue": "error"})
        artifact, = [event for event in trace["events"] if event["name"] == "artifact.link"]
        self.assertEqual(values(artifact)["factory.artifact.sha256"],
                         {"stringValue": hashlib.sha256(failure.read_bytes()).hexdigest()})
        for path in directory.iterdir():
            self.assertNotIn("private original diagnostic", path.read_text(encoding="utf-8"))

    def test_superseded_run_retains_native_result_and_all_otlp_receipts(self):
        pr = UPSTREAM + "/pull/2"
        self.store.park(self.task_id, "sylan", pr=pr)
        self.store.feedback(pr, "review", "review")

        def merge():
            self.store.feedback(pr, "merge", "merged")

        with self.assertRaises(Conflict):
            self.run_with(action=merge)
        run, = self.run_records()
        self.assertEqual(run["state"], "superseded")
        self.assertEqual(self.store.get(self.task_id)["state"], "closed")
        directory, trace, _, log = self.persisted_signals(run)
        self.assertTrue((directory / "result.txt").is_file())
        self.assertTrue((directory / "failure.json").is_file())
        self.assertIn("run.superseded", [event["name"] for event in trace["events"]])
        self.assertEqual(values(log)["factory.run.status"], {"stringValue": "error"})
