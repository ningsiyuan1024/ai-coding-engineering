import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from factory.state import Conflict, Store


PR = "https://github.com/Owner/Repo/pull/7"
PR_ALIAS = "https://github.com/owner/repo/pull/007/"
TRACE_ID = "abcdef0123456789" * 2


class StoreEdgeTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd()))
        self.root = Path(root)
        self.store = Store(self.root / "factory.db")

    def task(self, number, store=None):
        store = store or self.store
        task = str(uuid4())
        store.discover(task, f"T-{number}", f"https://github.com/owner/repo/issues/{number}", "Title")
        store.claim(task, "sylan", "rev-a", self.root / task, "rev-b")
        return task

    def begin(self, task, store=None, trace_id=None):
        return (store or self.store).begin(task, "sylan", "model", "medium", trace_id)

    def run_record(self, run, store=None):
        with (store or self.store).transaction() as db:
            return dict(db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())

    def test_old_decision_never_resumes_reopened_task(self):
        task = self.task(1)
        old = self.store.finish(
            task, self.begin(task), "waiting_human", question="First question?", pr=PR
        )
        self.store.feedback(PR, "pr-close", "closed")
        self.assertEqual(self.store.inbox(), [])
        self.store.feedback(PR_ALIAS, "pr-reopen", "reopened")
        current = self.store.finish(
            task, self.begin(task), "waiting_human", question="Current question?"
        )
        self.assertEqual([gate["id"] for gate in self.store.inbox()], [current])
        with self.assertRaises(Conflict):
            self.store.decide(old, "Take old action")
        self.assertEqual(self.store.get(task)["state"], "waiting_human")
        self.assertEqual(self.store.decide(current, "Take current action"), task)
        self.assertEqual(self.store.decide(current, "Take current action"), task)
        self.assertEqual(self.store.get(task)["state"], "ready_to_resume")

    def test_pr_aliases_cannot_change_owner(self):
        first, second = self.task(1), self.task(2)
        self.store.finish(first, self.begin(first), "waiting_upstream", pr=PR)
        second_run = self.begin(second)
        with self.assertRaisesRegex(Conflict, "PR already belongs"):
            self.store.finish(second, second_run, "waiting_upstream", pr=PR_ALIAS)
        self.assertEqual(self.store.get(second)["active_run"], second_run)
        reviewed = self.store.feedback(PR_ALIAS, "review-7", "review")
        self.assertEqual(reviewed["id"], first)
        self.assertEqual(reviewed["state"], "ready_to_resume")
        self.store.finish(first, self.begin(first), "waiting_upstream", pr=PR_ALIAS)
        self.assertEqual(self.store.get(first)["pr"], PR)

    def test_review_during_run_schedules_next_attempt_once(self):
        task = self.task(1)
        self.store.finish(task, self.begin(task), "waiting_upstream", pr=PR)
        self.store.feedback(PR, "first", "review")
        running = self.begin(task)
        state = self.store.feedback(PR, "during-run", "ci")
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["active_run"], running)
        self.assertEqual(self.store.feedback(PR_ALIAS, "during-run", "ci")["active_run"], running)
        self.store.finish(task, running, "waiting_upstream")
        self.assertEqual(self.store.get(task)["state"], "ready_to_resume")
        self.assertEqual(self.run_record(running)["state"], "waiting_upstream")
        self.store.finish(task, self.begin(task), "waiting_upstream")
        self.assertEqual(self.store.feedback(PR, "during-run", "ci")["state"], "waiting_upstream")

    def test_trace_can_be_pinned_after_supersession_and_cannot_be_replaced(self):
        task = self.task(1)
        first_run = self.begin(task, trace_id=TRACE_ID.upper())
        with self.assertRaisesRegex(Conflict, "different trace"):
            self.store.finish(task, first_run, "waiting_upstream", pr=PR, trace_id="b" * 32)
        self.assertEqual(self.store.get(task)["state"], "running")
        self.assertEqual(self.run_record(first_run)["trace_id"], TRACE_ID)
        self.store.finish(task, first_run, "waiting_upstream", pr=PR, trace_id=TRACE_ID)
        self.store.feedback(PR, "review", "review")
        next_run = self.begin(task)
        self.store.feedback(PR, "merge", "merged")
        self.store.attach_trace(next_run, TRACE_ID)
        self.store.attach_trace(next_run, TRACE_ID.upper())
        with self.assertRaisesRegex(Conflict, "different trace"):
            self.store.attach_trace(next_run, "b" * 32)
        with self.assertRaises(ValueError):
            self.store.attach_trace(next_run, "0" * 32)
        record = self.run_record(next_run)
        self.assertEqual(record["trace_id"], TRACE_ID)
        self.assertEqual(record["state"], "superseded")
        self.assertEqual(self.store.get(task)["state"], "closed")

    def test_legacy_database_keeps_pr_and_migrates_latest_pending_decision(self):
        path = self.root / "legacy.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, identifier TEXT NOT NULL, upstream TEXT NOT NULL,
                    upstream_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
                    owner TEXT, revision TEXT, worktree TEXT UNIQUE, base TEXT,
                    native_session TEXT, pr TEXT UNIQUE, active_run TEXT, updated TEXT NOT NULL
                );
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                    line TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                    state TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, trace_id TEXT
                );
                CREATE TABLE decisions (
                    id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                    prompt TEXT NOT NULL, answer TEXT, created TEXT NOT NULL, answered TEXT
                );
                INSERT INTO tasks VALUES (
                    'legacy', 'T-1', 'https://github.com/Owner/Repo/issues/1',
                    'owner/repo/1', 'Title', 'waiting_human', 'sylan', 'a', 'checkout',
                    'b', 'session', 'https://github.com/Owner/Repo/pull/7', NULL, '2026-11-27'
                );
                INSERT INTO decisions VALUES ('old', 'legacy', 'Old?', NULL, '2026-11-26', NULL);
                INSERT INTO decisions VALUES ('current', 'legacy', 'Current?', NULL, '2026-11-27', NULL);
                INSERT INTO runs VALUES ('prior', 'legacy', 'sylan', 'model', 'medium',
                    'waiting_human', '2026-11-27', '2026-11-27', NULL);
            """)
        store = Store(path)
        self.assertEqual([gate["id"] for gate in store.inbox()], ["current"])
        self.assertEqual(store.get("legacy")["pr"], PR)
        self.assertEqual(self.run_record("prior", store)["feedback_pending"], 0)
        with self.assertRaises(Conflict):
            store.decide("old", "Old answer")
        self.assertEqual(store.decide("current", "Current answer"), "legacy")
        run = self.begin("legacy", store=store)
        self.assertEqual(store.feedback(PR_ALIAS, "ci", "ci")["active_run"], run)
        store.finish("legacy", run, "waiting_upstream")
        self.assertEqual(Store(path).get("legacy")["state"], "ready_to_resume")

    def test_idle_owner_can_park_for_decision_and_link_pr(self):
        task = self.task(1)
        other = self.task(2)
        self.assertIsNone(self.store.latest_answer(task))
        with self.assertRaises(Conflict):
            self.store.park(task, "matt", question="Wrong owner?")
        gate = self.store.park(task, "sylan", question="Proceed?")
        self.assertEqual(self.store.park(task, "sylan", question="Proceed?"), gate)
        with self.assertRaises(Conflict):
            self.store.park(task, "sylan", question="A different question?")
        self.assertIsNone(self.store.park(task, "sylan", pr=PR))
        self.assertEqual(self.store.get(task)["state"], "waiting_human")
        self.assertEqual(self.store.get(task)["pr"], PR)
        self.store.decide(gate, "Proceed with the test")
        self.assertEqual(self.store.latest_answer(task), "Proceed with the test")
        self.assertIsNone(self.store.latest_answer(other))
        run = self.begin(task)
        with self.assertRaises(Conflict):
            self.store.park(task, "sylan", pr=PR)
        self.store.finish(task, run, "ready_to_resume")
        self.assertIsNone(self.store.park(task, "sylan", pr=PR_ALIAS))
        self.assertEqual(self.store.get(task)["state"], "waiting_upstream")
