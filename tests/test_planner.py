import json
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from factory import planner
from factory.state import Conflict


class PlannerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.seed_path = self.root / "model-plan.json"
        self.seed = {
            "schema_version": "1.0", "plan_id": "test-slice",
            "planner": {"requested": {"model": planner.MODEL, "reasoning_effort": planner.EFFORT}},
            "capacity": {"now": {"read_only_investigation_slots": 1, "implementation_slots": 0},
                         "after_headroom": {"implementation_slots": 1}},
            "candidates": [{"linear_id": "NIN-1", "url": "https://github.com/acme/pkg/issues/1",
                            "key": "acme/pkg#1"}],
            "model_allocation": {"line_requests": [{"lines": list(planner.NAMES),
                                 "model_request": "line-model", "effort_request": "medium"}]},
            "budget": {"investigation_per_admitted_issue": {"wall_minutes": 15,
                       "generated_tokens_review_at": 4000}, "one_implementation_attempt_after_admission":
                       {"wall_minutes_review_at": 45, "cumulative_generated_tokens_including_native_delegates_review_at": 16000}}}
        self.seed_path.write_text(json.dumps(self.seed), encoding="utf-8")
        with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
            db.execute("CREATE TABLE tasks(identifier, upstream_key, state, owner, revision, active_run)")
            db.execute("INSERT INTO tasks VALUES ('NIN-1','acme/pkg/1','ready',null,null,null)")
        self.cap = {"authenticated": True, "revision": "a"*64, "models": {"line-model": ["medium"]}}
        self.lines = patch.object(planner, "probe_lines", return_value={"sylan": self.cap})
        self.lines.start()
        self.host = patch.object(planner, "CodexRuntime").start()
        self.addCleanup(patch.stopall)
        self.rt = self.host.return_value.__enter__.return_value
        self.rt.capabilities.return_value = {"authenticated": True, "models": {planner.MODEL: ["ultra"]}}
        self.answer = {"revision": 1, "action": "offer", "reason": "Ready",
                       "option": 0, "mode": "investigate", "minutes": 10, "tokens": 2000}
        self.completed = True
        self.client = patch.object(planner, "ResponsesPlanner", autospec=True).start().return_value
        self.client.credentials_configured = True
        self.client.run_data_only.side_effect = self.turn

    def turn(self, prompt, model, effort, *, on_event):
        self.assertEqual((model, effort), (planner.MODEL, "ultra"))
        if self.completed:
            on_event("runtime.turn", {"model": model, "effort": effort, "status": "completed"})
        return SimpleNamespace(request_id="req_123", response_id="resp_123", text=json.dumps(self.answer))

    def decide(self, current=0, **kwargs):
        return planner.decide(self.root, self.seed_path, current, "capacity check", transport="responses", **kwargs)

    def test_dispatch_reads_initial_offer_and_hold(self):
        read = planner.dispatch_resources
        self.assertEqual(read(self.seed_path, "matt")["slots"], 1)
        self.assertEqual(read(self.seed_path, "matt", read_only=False)["slots"], 0)
        first = self.decide()
        offer = read(self.seed_path, "sylan")
        self.assertEqual((offer["sha256"], offer["slots"], offer["model"], offer["effort"]),
                         (first["sha256"], 1, "line-model", "medium"))
        self.assertEqual(read(self.seed_path, "lauren")["slots"], 0)
        self.assertEqual(read(self.seed_path, "sylan", read_only=False)["slots"], 0)
        self.answer = {"revision": 2, "action": "hold", "reason": "Review"}
        self.decide(1)
        self.assertEqual(read(self.seed_path, "sylan")["slots"], 0)
        self.seed_path.write_text(json.dumps(self.seed, indent=2))
        with self.assertRaises(Conflict):
            read(self.seed_path, "sylan")

    def test_revision_chain_replan_and_stale_request(self):
        first = self.decide()
        self.answer = {"revision": 2, "action": "hold", "reason": "Wait"}
        second = self.decide(1)
        proof = json.loads(Path(second["artifact"]).read_bytes())
        self.assertEqual(proof["payload"]["parent_sha256"], first["sha256"])
        self.assertEqual(proof["payload"]["transport"], "responses")
        self.assertEqual(proof["payload"]["response_id"], "resp_123")
        self.assertEqual(proof["payload"]["request_id"], "req_123")
        self.assertNotIn("turn_id", proof["payload"])
        self.assertNotIn("thread_id", proof["payload"])
        self.assertEqual(planner.history(Path(second["artifact"]).parent, "test-slice", 2), second["sha256"])
        with self.assertRaises(Conflict):
            self.decide(0)
        self.assertEqual(self.client.run_data_only.call_count, 2)
        Path(first["artifact"]).write_text('{"payload":{},"sha256":"wrong"}')
        with self.assertRaises(Conflict):
            self.decide(2)

    def test_check_planner_never_starts_a_turn(self):
        self.rt.capabilities.return_value = {"authenticated": False, "models": {planner.MODEL: ["ultra"]}}
        self.assertEqual(planner.check_planner(self.root / "planner-profile"),
                         {"authenticated": False, "advertised": True})
        self.client.run_data_only.assert_not_called()

    def test_transport_must_be_explicit(self):
        for value in (None, "native"):
            with self.assertRaises(ValueError):
                planner.decide(self.root, self.seed_path, 0, "initial", transport=value)
        self.client.run_data_only.assert_not_called()
        self.host.assert_not_called()

    def test_missing_dedicated_credentials_never_falls_back(self):
        self.client.credentials_configured = False
        with self.assertRaises(planner.RuntimeUnavailable):
            self.decide()
        self.client.run_data_only.assert_not_called()
        self.host.assert_not_called()
        failure, = self.root.rglob("failure.json")
        proof = json.loads(failure.read_bytes())
        self.assertEqual((proof["status"], proof["transport"]), ("failed", "responses"))
        self.assertFalse(proof["credentials_configured"])
        self.assertNotIn("response_id", proof)
        self.assertFalse(list(self.root.rglob("00000001.json")))

    def test_real_responses_adapter_to_dispatch_with_in_memory_http(self):
        from factory import planner_transport

        response = MagicMock(status=200)
        response.getheader.return_value = "req_fixture"
        response.read.return_value = json.dumps({
            "id": "resp_fixture", "object": "response", "status": "completed",
            "model": planner.MODEL, "reasoning": {"effort": planner.EFFORT},
            "store": False, "tools": [], "tool_choice": "none",
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(self.answer)}]}],
        }).encode()
        adapter = planner_transport.ResponsesPlanner(api_key="fixture")
        with patch.object(planner, "ResponsesPlanner", return_value=adapter), \
                patch.object(planner_transport, "HTTPSConnection") as dial:
            conn = dial.return_value
            conn.getresponse.return_value = response
            result = self.decide()
        method, path, body, _ = conn.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/responses"))
        request = json.loads(body)
        self.assertEqual(request["tools"], [])
        self.assertFalse(request["store"])
        self.assertNotIn("acme/pkg", request["input"])
        receipt = json.loads(Path(result["artifact"]).read_bytes())["payload"]
        self.assertEqual((receipt["request_id"], receipt["response_id"]), ("req_fixture", "resp_fixture"))
        self.assertEqual(planner.dispatch_resources(self.seed_path, "sylan")["revision"], 1)
        self.host.assert_not_called()

    def test_unconfirmed_completion_does_not_publish(self):
        for key, value in (("model", "other"), ("effort", "medium"), ("status", "incomplete")):
            def mismatch(prompt, model, effort, *, on_event):
                fields = {"model": model, "effort": effort, "status": "completed", key: value}
                on_event("runtime.turn", fields)
                return SimpleNamespace(request_id="req_123", response_id="resp_123", text="{}")
            self.client.run_data_only.side_effect = mismatch
            with self.subTest(key=key), self.assertRaises(planner.RuntimeUnavailable):
                self.decide()
        self.assertFalse(list(self.root.rglob("00000001.json")))

    def test_invalid_output_never_becomes_a_revision(self):
        for change in ({"revision": 2}, {"action": "claim"}, {"tokens": True}, {"minutes": 16},
                       {"tokens": 4001}, {"profile_revision": "other"}, {"model": "unlisted"},
                       {"effort": "high"}, {"extra": "field"}, {"reason": ""}):
            with self.subTest(change=change), patch.dict(self.answer, change), self.assertRaises(ValueError):
                self.decide()
        self.completed = False
        with self.assertRaises(planner.RuntimeUnavailable):
            self.decide()
        self.assertFalse(list(self.root.rglob("00000001.json")))

    def test_capacity_claim_or_auth_change_invalidates_offer(self):
        with patch.dict(self.cap, authenticated=False), self.assertRaises(ValueError):
            self.decide()
        with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
            db.execute("INSERT INTO tasks VALUES ('OTHER','other/repo/2','running','matt','rev','run')")
        with self.assertRaises(ValueError):
            self.decide()
        with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
            db.execute("DELETE FROM tasks WHERE identifier='OTHER'")
        def claim_before_commit(*args, **kwargs):
            result = self.turn(*args, **kwargs)
            with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
                db.execute("UPDATE tasks SET state='claimed',owner='matt'")
            return result
        self.client.run_data_only.side_effect = claim_before_commit
        with self.assertRaises(ValueError):
            self.decide()

    def test_disk_readmission_requires_measured_headroom_and_peak(self):
        self.answer["mode"] = "implement"
        with patch.object(planner.shutil, "disk_usage", return_value=SimpleNamespace(free=4*1024**3)):
            with self.assertRaises(ValueError):
                self.decide()
            with self.assertRaises(ValueError):
                self.decide(estimated_peak_bytes=2048)
        with patch.object(planner.shutil, "disk_usage", return_value=SimpleNamespace(free=4*1024**3+2048)):
            with self.assertRaises(ValueError):
                self.decide(estimated_peak_bytes=2048)
            self.answer["mode"] = "investigate"
            self.decide()
            self.answer.update(mode="implement", revision=2)
            self.assertEqual(self.decide(1, estimated_peak_bytes=2048)["decision"]["mode"], "implement")

    def test_racing_writer_cannot_overwrite_winning_revision(self):
        def race(*args, **kwargs):
            result = self.turn(*args, **kwargs)
            self.client.run_data_only.side_effect = self.turn
            self.winner = self.decide()
            return result
        self.client.run_data_only.side_effect = race
        with self.assertRaises(Conflict):
            self.decide()
        self.assertEqual(json.loads(Path(self.winner["artifact"]).read_bytes())["sha256"], self.winner["sha256"])

    def test_resume_with_no_unclaimed_candidate_keeps_original_owner_revision(self):
        with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
            db.execute("UPDATE tasks SET state='ready_to_resume',owner='sylan',revision=?", ("a"*64,))
        self.assertEqual(self.decide()["decision"]["line"], "sylan")
        self.answer["revision"] = 2
        with closing(sqlite3.connect(self.root / "factory.db")) as db, db:
            db.execute("UPDATE tasks SET owner='matt'")
        with self.assertRaises(ValueError):
            self.decide(1)

    def test_model_boundary_contains_only_anonymous_fields_on_replan(self):
        private_reason = "private prior reason NIN-1"
        raw_event = "private GitHub request from user at " + str(self.root)
        self.answer["reason"] = private_reason
        self.decide()
        self.answer.update(revision=2, reason="wait")
        result = planner.decide(self.root, self.seed_path, 1, raw_event, transport="responses")
        prompt = self.client.run_data_only.call_args.args[0]
        for private in (private_reason, raw_event, str(self.root), "acme/pkg", "NIN-1",
                        "line-model", "test-slice", "sylan", "a"*64):
            self.assertNotIn(private, prompt)
        public = json.loads(prompt.split(planner.GUIDANCE + "\n")[1])
        self.assertEqual(public, {
            "revision": 2, "event": "other", "ready": 1, "active": 0,
            "slots": {"investigate": 1, "implement": 0},
            "limits_minutes_tokens": {"investigate": [15, 4000], "implement": [45, 16000]},
            "options": [{"option": 0, "lane": 0, "preferred": True, "effort": "medium",
                         "owned": 0, "resumable": 0}]})
        evidence = json.loads(Path(result["artifact"]).read_bytes())["payload"]
        self.assertEqual(evidence["context"]["trigger"], raw_event)
        self.assertEqual(evidence["context"]["previous"]["reason"], private_reason)

    def test_seed_and_revision_budget_dispatch(self):
        read = planner.dispatch_resources
        self.assertEqual(read(self.seed_path,"sylan")["tokens"], 4000)
        self.assertEqual(read(self.seed_path,"sylan",read_only=False)["tokens"], 0)
        self.decide()
        actual = read(self.seed_path,"sylan")
        self.assertEqual((actual["minutes"], actual["tokens"]), (10, 2000))
        self.assertEqual(read(self.seed_path,"matt")["tokens"], 0)
        self.seed["capacity"]["now"]["implementation_slots"] = 2
        self.seed_path.write_text(json.dumps(self.seed))
        with self.assertRaises(ValueError):
            read(self.seed_path, "matt")

    def test_malformed_seed_and_duplicate_keys(self):
        with self.assertRaises(ValueError):
            planner.decode('{"revision":1,"revision":2}')
        self.seed["capacity"]["now"]["read_only_investigation_slots"] = True
        self.seed_path.write_text(json.dumps(self.seed))
        with self.assertRaises(ValueError):
            self.decide()
        self.host.assert_not_called()

    def test_doctor_probe_uses_pinned_profile_and_live_auth(self):
        self.lines.stop()
        d = self.root / "probes" / "id"
        d.mkdir(parents=True)
        (d / "discovery.json").write_text(json.dumps({"line":"sylan", "host":"codex", "revision":"b"*64,
                                                     "authenticated":True}))
        native = MagicMock()
        native.__enter__.return_value.capabilities.return_value = {"authenticated": False}
        with patch.object(planner.runner, "profile", return_value=("b"*64, d, None)) as pinned, \
                patch.object(planner.runner, "host", return_value=native):
            actual = planner.probe_lines(self.root)
        self.assertEqual(pinned.call_args.kwargs["pinned"], "b"*64)
        self.assertFalse(actual["sylan"]["authenticated"])
        self.assertIsNone(actual["matt"]["authenticated"])

    def test_cli_profiles_reaches_initial_probe_and_offer_readback(self):
        profiles = self.root / "external" / "profiles"
        probes = planner.probe_lines
        with patch("builtins.print"):
            code = planner.main(["--root", str(self.root), "--profiles", str(profiles),
                                 "--transport", "responses", "--expected-revision", "0",
                                 "--trigger", "initial-capacity-check"])
        self.assertEqual(code, 0)
        self.assertEqual([call.args for call in probes.call_args_list],
                         [(self.root, profiles), (self.root, profiles)])

    def test_reference_lines_only_probe_matching_native_host_and_profile(self):
        self.lines.stop()
        profiles = self.root / "profiles"
        revision = "d" * 40
        for folder, host, base in (("1", "codex", profiles), ("2", "claude", profiles),
                                   ("3", "codex", self.root / "unrelated")):
            for line in ("matt", "addy", "lauren"):
                d = self.root / "probes" / (folder + line)
                d.mkdir(parents=True)
                (d / "discovery.json").write_text(json.dumps({
                    "line": line, "revision": revision if folder == "1" else "a"*40,
                    "profiles": str(base), "host": host}))
        native = MagicMock()
        rt = native.__enter__.return_value
        rt.capabilities.return_value = {"authenticated": True, "models": {"gpt-6.2-sol":["medium"]}}
        with patch.object(planner.runner, "profile", return_value=(revision, profiles, {})) as pin, \
                patch.object(planner.runner, "host", return_value=native):
            result = planner.probe_lines(self.root, profiles)
            self.assertEqual({call.args[0] for call in pin.call_args_list}, {"matt", "addy"})
            for call in pin.call_args_list:
                self.assertEqual((call.args[1], call.kwargs["pinned"]), (profiles, revision))
            for line in ("matt", "addy"):
                self.assertEqual(result[line]["models"], {"gpt-6.2-sol":["medium"]})
                self.assertTrue(result[line]["authenticated"])
            self.assertEqual(result["lauren"]["models"], {})
            for malformed in ({}, None, {"gpt-6.2-sol": "medium"}):
                rt.capabilities.return_value = {"authenticated": True, "models": malformed}
                result = planner.probe_lines(self.root, profiles)
                self.assertEqual(result["matt"]["models"], {})
                self.assertTrue(result["matt"]["authenticated"])
        rt.discover.assert_not_called()

    def test_readonly_native_seed_and_held_cursor_apply_to_offers(self):
        self.seed["schema_version"] = "1.1"
        self.seed["capacity"]["now"].update(heavy_build_slots=0, gpu_or_paid_benchmark_calls=0)
        self.seed["capacity"]["after_headroom"]["implementation_slots"] = 0
        requests = [{"lines":[label], "host":planner.NATIVE_HOSTS[line],
                     "model_request":"gpt-6.2-sol", "effort_request":"medium"}
                    for label,line in planner.NAMES.items()]
        self.seed["model_allocation"]["line_requests"] = requests
        self.seed_path.write_text(json.dumps(self.seed))
        self.assertEqual(planner.dispatch_resources(self.seed_path, "lauren")["slots"], 0)
        self.assertEqual(planner.dispatch_resources(self.seed_path, "matt")["slots"], 1)
        self.cap["models"] = {model:["medium"] for model in
                              ("claude-opus", "auto", "sonnet", "default", "gpt-6.2-sol", "other")}
        proof = self.decide()
        self.assertEqual(proof["decision"]["model"], "gpt-6.2-sol")
        view = json.loads(self.client.run_data_only.call_args.args[0].split(planner.GUIDANCE + "\n")[1])
        self.assertEqual(len(view["options"]), 1)
        for bad in ("claude-opus","sonnet","anthropic/haiku","auto","default"):
            requests[0]["model_request"] = bad
            self.seed_path.write_text(json.dumps(self.seed))
            with self.subTest(model=bad), self.assertRaises(ValueError):
                planner.read_seed(self.seed_path)
        requests[0]["model_request"] = "gpt-6.2-sol"
        requests[0]["host"] = "claude"
        self.seed_path.write_text(json.dumps(self.seed))
        with self.assertRaises(ValueError):
            planner.read_seed(self.seed_path)
        requests[0]["host"] = "codex"
        self.seed["capacity"]["after_headroom"]["implementation_slots"] = 1
        self.seed_path.write_text(json.dumps(self.seed))
        with self.assertRaises(ValueError):
            planner.read_seed(self.seed_path)


if __name__ == "__main__":
    unittest.main()
