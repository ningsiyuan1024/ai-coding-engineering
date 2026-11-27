import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from factory import dispatch
from factory.runtime import RuntimeUnavailable
from factory.state import Conflict, LINES, Store


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.store = Store(self.root / "factory.db")
        self.uid = str(uuid4())
        self.store.discover(self.uid, "T-1", "https://github.com/owner/repo/issues/5", "Task")
        self.sources = {"owner/repo": {"path": self.root / "bare.git", "base": "base"}}
        self.policy = {"slots": 1, "revision": 0, "model": "example", "effort": "medium",
                       "sha256": "seed", "installed_revision": "pin"}

    def tick(self):
        return dispatch.tick(self.store, self.root, self.root / "profiles", self.root / "plan", self.sources)

    def test_admission_closes_before_a_scout_turn_if_hosts_cannot_log_in(self):
        with patch.object(dispatch, "route_available", return_value=(self.policy, "native_login_required")), \
                patch("factory.runner.choose") as choose, patch("factory.eligibility.read_candidate") as github:
            self.assertEqual(self.tick()["lines"], dict.fromkeys(LINES, "native_login_required"))
            choose.assert_not_called()
            github.assert_not_called()
            self.assertIsNone(self.store.get(self.uid)["owner"])

    def test_locked_worker_still_receives_maintenance_and_status(self):
        sources = self.root / "sources.json"
        sources.write_text("{}")
        receipt = self.root / "status.json"
        with dispatch.admission(self.store.path.with_suffix(".dispatch.lock")), \
                patch.object(dispatch, "linear_scope", return_value=(set(), "test_scope")), \
                patch("factory.maintenance.maintain", return_value={"github":"checked"}) as maintain, \
                patch.object(dispatch.runner, "choose") as scout:
            result = dispatch.main(["--root", str(self.root), "--sources", str(sources),
                                    "--maintain", "--receipt", str(receipt)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(receipt.read_bytes())["result"],
                         {"state":"occupied", "maintenance":{"github":"checked"}, "intake":"test_scope"})
        maintain.assert_called_once()
        scout.assert_not_called()

    def test_unavailable_line_identity_does_not_block_other_hosts(self):
        good = MagicMock()
        good.__enter__.return_value.capabilities.return_value = {"authenticated":True,"models":{"example":["medium"]}}
        policy = {**self.policy, "profile_revision":None}
        with patch.object(dispatch, "dispatch_resources", return_value=policy), \
                patch.object(dispatch.runner, "profile", return_value=("pin", self.root, None)), \
                patch.object(dispatch.runner, "host", side_effect=[RuntimeUnavailable("missing identity"), good, good, good]), \
                patch("factory.eligibility.read_candidate", return_value={"blocked":False,"revision":"v1","context":{}}), \
                patch.object(dispatch.runner, "choose", return_value=None) as choose:
            result = self.tick()
        self.assertEqual(result["lines"]["sylan"], "native_host_unavailable")
        self.assertEqual([call.args[1] for call in choose.call_args_list], ["matt","lauren","addy"])

    def test_legacy_and_automatic_model_requests_never_open_a_host(self):
        with patch.object(dispatch.runner, "profile") as profile:
            for name in ("sonnet", "claude-sonnet-4-6", "anthropic/claude", "opus[1m]", "fable",
                         "Fable-5", "auto", "default"):
                with self.subTest(model=name), patch.object(dispatch, "dispatch_resources",
                                                            return_value={**self.policy, "model":name}):
                    _, why = dispatch.route_available(self.root, self.root, self.root / "plan", "addy", True)
                    self.assertEqual(why, "model_replan_required")
            profile.assert_not_called()

    def test_route_requires_the_runtime_to_advertise_model_and_effort(self):
        caps = MagicMock()
        with patch.object(dispatch, "dispatch_resources", return_value={**self.policy, "profile_revision":None}), \
                patch.object(dispatch.runner, "profile", return_value=("pin", self.root, {})), \
                patch.object(dispatch.runner, "host", return_value=caps):
            for models in (None, {}, {"example":None}, {"example":["low"]}):
                caps.__enter__.return_value.capabilities.return_value = {"authenticated":True, "models":models}
                self.assertEqual(dispatch.route_available(self.root, self.root, self.root / "plan", "addy", True)[1],
                                 "model_replan_required")

    def test_claim_requires_unchanged_live_upstream_evidence(self):
        snapshots = [{"blocked": False, "revision": "first", "context": {}},
                     {"blocked": True, "revision": "new-pr", "context": {}}]
        with patch.object(dispatch, "route_available", return_value=(self.policy, None)), \
                patch("factory.runner.choose", return_value=self.uid), \
                patch("factory.eligibility.read_candidate", side_effect=snapshots), \
                patch("factory.runner.run") as run:
            self.assertEqual(self.tick()["state"], "upstream_changed")
            run.assert_not_called()
            self.assertIsNone(self.store.get(self.uid)["owner"])

    def test_unchanged_declines_are_not_billed_again(self):
        observation = {"blocked": False, "revision": "open", "context": {}}
        with patch.object(dispatch, "route_available", return_value=(self.policy, None)), \
                patch("factory.eligibility.read_candidate", return_value=observation), \
                patch("factory.runner.choose", return_value=None) as choose:
            for _ in range(2):
                self.assertEqual(self.tick()["lines"], dict.fromkeys(LINES, "declined"))
            self.assertEqual(choose.call_count, len(LINES))

    def test_claim_race_recovery_and_no_paid_replay(self):
        def claim(line):
            try:
                self.store.claim(self.uid, line, "pin", self.root / self.uid, "base")
                return line
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=4) as workers:
            owners = [owner for owner in workers.map(claim, LINES) if owner]
        owner, = owners
        self.assertTrue(self.store.needs_turn(self.uid))
        first = self.store.begin(self.uid, owner, "model", "high")
        self.store.bind(self.uid, first, "session")
        with self.assertRaises(Conflict):
            self.store.recover(self.uid, "stale")
        self.store.recover(self.uid, first)
        self.assertTrue(self.store.needs_turn(self.uid))
        second = self.store.begin(self.uid, owner, "model", "medium")
        self.assertEqual(self.store.get(self.uid)["native_session"], "session")
        self.store.finish(self.uid, second, "ready_to_resume")
        self.assertFalse(self.store.needs_turn(self.uid))
        self.store.request_turn(self.uid, "Run the independent verifier")
        self.assertTrue(self.store.needs_turn(self.uid))
