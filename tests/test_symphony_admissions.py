"""Boundary between factory ownership/admission and Symphony's engineering worker."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from factory import dispatch, runner
from factory.state import Conflict, Store


class SymphonyAdmissions(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd())))
        self.store = Store(self.root / "factory.db")
        self.repo = self.root / "upstream"
        self.profiles = self.root / "profiles"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false",
                        "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        # Identity only. No operation fetches from or publishes to this remote.
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/example/project.git"], check=True)
        self.base = runner.git(self.repo, "rev-parse", "HEAD")
        self.sources = {"example/project": {"path": self.repo, "base": self.base}}
        self.pin = runner.profile("sylan", self.profiles)[0]
        self.policy = {"slots": 1, "revision": 1, "model": "fixture", "effort": "medium",
                       "sha256": "fixture-plan", "installed_revision": self.pin}
        self.enterContext(patch.object(dispatch, "route_available", side_effect=self.route))
        self.check_policy = self.enterContext(patch.object(dispatch, "dispatch_resources", return_value=self.policy))
        self.choose = self.enterContext(patch.object(runner, "choose", side_effect=self.pick))
        self.enterContext(patch("factory.eligibility.read_candidate", return_value={
            "blocked": False, "revision": "consistent-public-state", "context": {},
        }))
        self.run_engineer = self.enterContext(patch.object(
            runner, "run", side_effect=AssertionError("admission invoked the engineering runtime")))

    def route(self, root, profiles, plan, line, read_only, *, pinned=None):
        return self.policy, None if line == "sylan" else "fixture_unavailable"

    @staticmethod
    def pick(*args, evidence, pinned):
        return next(iter(evidence))

    def discover(self, number):
        uid = str(uuid4())
        self.store.discover(uid, f"NIN-{number}", f"https://github.com/example/project/issues/{number}", "fixture")
        return uid

    def tick(self, ids, **kwargs):
        return dispatch.tick(self.store, self.root, self.profiles, self.root / "plan.json",
                             self.sources, read_only=True, eligible_ids=ids, **kwargs)

    def test_claim_is_prepared_once_and_exiting_scope_does_not_select_or_execute(self):
        scoped, other = self.discover(1), self.discover(2)
        self.tick(set())
        self.choose.assert_not_called()
        self.tick({scoped})
        row = self.store.get(scoped)
        self.assertEqual((row["owner"], row["revision"], row["base"]), ("sylan", self.pin, self.base))
        self.assertEqual(runner.git(row["worktree"], "rev-parse", "HEAD"), self.base)
        self.assertIsNone(self.store.get(other)["owner"])
        self.assertEqual(set(self.choose.call_args.kwargs["evidence"]), {scoped})
        self.tick({scoped})
        self.tick(set())
        self.assertEqual(self.choose.call_count, 1)
        self.assertEqual(self.store.get(scoped)["worktree"], row["worktree"])
        self.run_engineer.assert_not_called()

    def test_racing_scouts_leave_one_pinned_owner_and_one_worktree(self):
        uid = self.discover(1)
        started, release = Event(), Event()

        def slow_choice(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError("Fixture admission did not release")
            return uid

        self.choose.side_effect = slow_choice
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.tick, {uid})
            try:
                self.assertTrue(started.wait(10))
                try:
                    self.tick({uid})
                except Conflict:
                    pass
            finally:
                release.set()
            first.result()
        self.tick({uid})
        self.assertEqual(self.choose.call_count, 1)
        self.assertEqual(len(list((self.root / "worktrees").iterdir())), 1)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE type='claimed'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.run_engineer.assert_not_called()

    def test_scope_and_resource_changes_during_selection_prevent_claim(self):
        uid = self.discover(1)
        self.assertEqual(self.tick({uid}, refresh_scope=lambda: set())["state"], "linear_scope_changed")
        self.check_policy.return_value = self.policy | {"slots": 0, "sha256": "new-plan"}
        self.assertEqual(self.tick({uid}, refresh_scope=lambda: {uid})["state"], "plan_changed")
        self.assertIsNone(self.store.get(uid)["owner"])
        self.assertFalse((self.root / "worktrees" / uid).exists())
        self.run_engineer.assert_not_called()

    def test_prior_owner_leaving_backlog_does_not_starve_other_lines(self):
        prior, new = self.discover(1), self.discover(2)
        previous_tree = self.root / "worktrees" / prior
        previous_pin = "a" * 40
        self.store.claim(prior, "matt", previous_pin, previous_tree, self.base)
        runner.prepare_worktree(self.repo, previous_tree, self.base)
        self.assertTrue(self.store.needs_turn(prior))
        self.tick({new})
        retained = self.store.get(prior)
        self.assertEqual((retained["owner"], retained["revision"], retained["worktree"]),
                         ("matt", previous_pin, str(previous_tree)))
        self.assertEqual(runner.git(previous_tree, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.store.get(new)["owner"], "sylan")
        self.assertEqual(set(self.choose.call_args.kwargs["evidence"]), {new})
        self.run_engineer.assert_not_called()

    def test_crashed_after_sqlite_claim_repairs_missing_tree_at_original_base(self):
        uid = self.discover(1)
        tree = self.root / "worktrees" / uid
        self.store.claim(uid, "sylan", self.pin, tree, self.base)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false",
                        "commit", "-q", "--allow-empty", "-m", "advanced"], check=True)
        self.sources["example/project"]["base"] = "HEAD"
        self.tick({uid})
        self.choose.assert_not_called()
        self.assertNotEqual(runner.git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(runner.git(tree, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.store.get(uid)["revision"], self.pin)
        self.run_engineer.assert_not_called()

    def test_engineering_slot_human_decision_and_pr_replay_preserve_native_identity(self):
        first, candidate = self.discover(1), self.discover(2)
        self.tick({first})
        row = self.store.get(first)
        run = self.store.begin(first, "sylan", "fixture", "medium")
        self.store.bind(first, run, "fixture-original-session")
        self.tick({first, candidate})
        self.assertEqual(self.choose.call_count, 1)
        self.assertIsNone(self.store.get(candidate)["owner"])
        gate = self.store.finish(first, run, "waiting_human", question="Choose the documented expected result")
        self.tick({first})
        self.assertFalse(self.store.needs_turn(first))
        self.store.decide(gate, "Use the documented result")
        self.tick({first})
        self.assertTrue(self.store.needs_turn(first))
        pr = "https://github.com/example/project/pull/42"
        self.store.park(first, "sylan", pr=pr)
        self.store.feedback(pr, "github:review:fixture", "review")
        self.store.feedback(pr, "github:review:fixture", "review")
        self.store = Store(self.store.path)
        self.tick({first})
        after = self.store.get(first)
        self.assertEqual(after["state"], "ready_to_resume")
        self.assertEqual(tuple(after[key] for key in ("owner","revision","base","worktree")),
                         tuple(row[key] for key in ("owner","revision","base","worktree")))
        self.assertEqual(after["native_session"], "fixture-original-session")
        self.assertEqual(self.choose.call_count, 1)
        self.run_engineer.assert_not_called()
