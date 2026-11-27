from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from factory import github_events, maintenance
from factory.state import Store


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.profiles = self.root / "profiles"
        self.store = Store(self.root / "factory.db")
        self.clock = patch.object(maintenance.time, "time", return_value=10000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def maintain(self):
        return maintenance.maintain(self.store, self.root, self.profiles)

    def mark_provisioned(self, line):
        native = self.profiles / line / "native"
        (native / "owner").mkdir(parents=True)
        (native / "owner" / line).touch()
        db = sqlite3.connect(native / "versions.sqlite3")
        try:
            with db:
                db.execute("CREATE TABLE versions (revision TEXT)")
                db.execute("INSERT INTO versions VALUES ('v1')")
        finally:
            db.close()

    def test_weekly_sync_preserves_running_task_pin_and_ignores_unprovisioned_lines(self):
        task_id = str(uuid4())
        self.store.discover(task_id, "T-1", "https://github.com/example/project/issues/1", "task")
        self.store.claim(task_id, "matt", "v1", self.root / "task", "base")
        self.store.begin(task_id, "matt", "model", "effort")
        self.mark_provisioned("matt")
        with patch.object(maintenance.native_skills, "sync", return_value=SimpleNamespace(revision="v2")) as sync:
            first = self.maintain()
            self.assertEqual(first["profiles"]["matt"]["revision"], "v2")
            self.assertEqual(first["profiles"]["addy"]["state"], "unprovisioned")
            self.assertFalse((self.profiles / "addy").exists())
            self.now.return_value += maintenance.SYNC_INTERVAL - 1
            self.store = Store(self.store.path)
            self.assertEqual(self.maintain()["profiles"]["matt"]["state"], "deferred")
            self.now.return_value += 1
            self.assertEqual(self.maintain()["profiles"]["matt"]["state"], "ok")
            self.assertEqual(sync.call_count, 2)
        active = self.store.get(task_id)
        self.assertEqual((active["state"], active["revision"]), ("running", "v1"))

    def test_failed_and_partial_jobs_back_off_independently(self):
        self.mark_provisioned("matt")
        self.mark_provisioned("addy")
        def refresh(line, profile):
            if line == "matt":
                raise OSError("private detail")
            return SimpleNamespace(revision="v2")
        report = {"pull_requests": [], "errors": [{"error":"fixture"}]}
        with patch.object(maintenance.github_events, "poll_once", return_value=report) as poll, \
             patch.object(maintenance.native_skills, "sync", side_effect=refresh):
            first = self.maintain()
            self.assertEqual(first["github"]["state"], "partial")
            self.assertEqual(first["profiles"]["addy"]["state"], "ok")
            self.assertEqual(first["profiles"]["matt"]["error_type"], "OSError")
            self.assertNotIn("private detail", str(first))
            self.now.return_value += maintenance.RETRY_INTERVAL - 1
            self.assertEqual(self.maintain()["github"]["state"], "deferred")
            self.now.return_value += 1
            next_attempt = self.maintain()
            self.assertEqual(next_attempt["profiles"]["addy"]["state"], "deferred")
            self.assertEqual(next_attempt["github"]["next_due"], self.now.return_value + 2*maintenance.RETRY_INTERVAL)
            self.assertEqual(poll.call_count, 2)

    def test_poll_replay_routes_one_durable_event(self):
        task_id = str(uuid4())
        self.store.discover(task_id, "T-1", "https://github.com/example/project/issues/1", "task")
        self.store.claim(task_id, "lauren", "v1", self.root / "task", "base")
        self.store.park(task_id, "lauren", pr="https://github.com/example/project/pull/5")
        real_poll = github_events.poll_once
        def poll(store):
            return real_poll(store, source=lambda _: [github_events.Event("review", "github:one")])
        with patch.object(maintenance.github_events, "poll_once", side_effect=poll):
            for _ in range(2):
                self.assertEqual(self.maintain()["github"]["events_observed"], 1)
                self.now.return_value += maintenance.PR_INTERVAL
        with self.store.transaction() as db:
            count = db.execute("SELECT count(*) FROM events WHERE key='github:one'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.store.get(task_id)["state"], "ready_to_resume")

    def test_concurrent_watch_does_not_duplicate_network_work(self):
        entered, release = Event(), Event()
        def hold(store):
            entered.set()
            release.wait(5)
            return {"pull_requests": [], "errors": []}
        with patch.object(maintenance.github_events, "poll_once", side_effect=hold) as poll, \
             ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.maintain)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.maintain()["github"]["state"], "busy")
            finally:
                release.set()
            self.assertEqual(first.result()["github"]["state"], "ok")
            self.assertEqual(poll.call_count, 1)
