import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory import planner, runner, symphony_worker as worker
from factory.state import Store
from factory.worker_process import port_cancelled, wait_for_start


def _fixture(action, task, *args):
    config = worker.Config.load()
    script = str(Path(__file__).resolve())
    if action == "parent":
        child = subprocess.Popen([sys.executable, script, "guardian", task], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                 start_new_session=os.name != "nt")
        child.wait()  # Simulates the external Port owner retaining the life pipe.
    elif action == "guardian":
        def command(_, task_id, token):
            return [sys.executable, script, "turn", task_id, token]
        result = worker.guard(config, task, port_cancelled(), command=command)
        runner.write_once(config.root / "guard-result", runner.json_bytes(result))
    elif action == "turn":
        if not wait_for_start():
            return
        token, = args
        store = worker._RunStore(config.state, token)
        run = store.begin(task, "sylan", "fixture", "low")
        store.bind(task, run, "fixture-session")
        leaf = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"],
                                stdin=subprocess.DEVNULL, close_fds=True)
        runner.write_once(config.root / "started", runner.json_bytes(
            {"run": run, "executor": os.getpid(), "leaf": leaf.pid}))
        leaf.wait()
    elif action == "unavailable" and wait_for_start():
        print(json.dumps({"disposition":"idle", "reason":"native_login_required"}), flush=True)


def _alive(pid):
    if os.name == "nt":
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return k.WaitForSingleObject(handle, 0) == 258
        finally:
            k.CloseHandle(handle)
    stat_file = Path(f"/proc/{pid}/stat")
    if stat_file.exists() and stat_file.read_text().split(") ")[-1].startswith("Z "):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class SymphonyWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.uid = str(uuid.uuid4())
        self.store = Store(self.root / "factory.db")
        self.store.discover(self.uid, "NIN-1", "https://github.com/example/repo/issues/1", "test")
        profiles = self.root / "profiles"
        revision, _, _ = runner.profile("sylan", profiles)
        self.worktree = self.root / "worktrees" / self.uid
        self.worktree.mkdir(parents=True)
        self.store.claim(self.uid, "sylan", revision, self.worktree, "base")
        plan = self.root / "model-plan.json"
        plan.write_text(json.dumps({
            "schema_version":"1.0", "plan_id":"worker-test",
            "planner":{"requested":{"model":planner.MODEL,"reasoning_effort":planner.EFFORT}},
            "capacity":{"now":{"read_only_investigation_slots":1,"implementation_slots":0}},
            "candidates":[], "model_allocation":{"line_requests":[{"lines":list(planner.NAMES),
                        "model_request":"fixture","effort_request":"low"}]},
            "budget":{"investigation_per_admitted_issue":{"wall_minutes":1,"generated_tokens_review_at":12},
                      "one_implementation_attempt_after_admission":{"wall_minutes_review_at":1,
                      "cumulative_generated_tokens_including_native_delegates_review_at":12}},
        }), encoding="utf-8")
        self.config = worker.Config(self.root, self.store.path, profiles, plan, True)
        self.env = {**os.environ, "FACTORY_ROOT":str(self.root), "FACTORY_STATE":str(self.store.path),
                    "FACTORY_PROFILES":str(profiles), "FACTORY_PLAN":str(plan), "FACTORY_MODE":"investigate"}

    def wait(self, predicate):
        until = time.monotonic() + 35
        while not predicate():
            if time.monotonic() > until:
                self.fail("Timed out waiting for native worker evidence")
            time.sleep(0.05)

    def launch(self, action):
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), action, self.uid],
                                 env=self.env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, close_fds=True)
        def cleanup():
            if not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stderr.close()
        self.addCleanup(cleanup)
        self.wait(lambda: (self.root / "started").exists() or child.poll() is not None)
        if child.poll() is not None:
            self.fail(child.stderr.read().decode(errors="replace"))
        return child, json.loads((self.root / "started").read_bytes())

    def test_parent_death_stops_real_descendants_and_recovers_only_its_run(self):
        parent, actual = self.launch("parent")
        self.assertFalse(worker.eligible(self.config, self.uid)["eligible"])
        self.assertTrue(_alive(actual["leaf"]))
        parent.kill()
        parent.wait()
        self.wait(lambda: (self.root / "guard-result").exists())
        self.assertFalse(_alive(actual["executor"]))
        self.assertFalse(_alive(actual["leaf"]))
        self.assertEqual(json.loads((self.root / "guard-result").read_bytes())["reason"], "cancelled")
        self.assertTrue(self.store.needs_turn(self.uid))
        self.assertEqual(self.store.get(self.uid)["native_session"], "fixture-session")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT state FROM runs WHERE id=?", (actual["run"],)).fetchone()[0],
                             "interrupted")
        later = self.store.begin(self.uid, "sylan", "fixture", "low", require_wakeup=True)
        worker._recover_owned(self.store, self.uid, actual["run"])
        self.assertEqual(self.store.get(self.uid)["active_run"], later)
        self.store.bind(self.uid, later, "fixture-session")

    @unittest.skipUnless(os.name == "nt", "Verifies Windows kernel Job Object kill-on-close")
    def test_guardian_kill_stops_the_job_and_next_poll_recovers_the_receipt(self):
        guardian, actual = self.launch("guardian")
        guardian.kill()
        guardian.wait()
        self.wait(lambda: not _alive(actual["executor"]) and not _alive(actual["leaf"]))
        self.assertTrue(worker.eligible(self.config, self.uid)["eligible"])
        self.assertIsNone(self.store.get(self.uid)["active_run"])
        self.assertEqual(self.store.get(self.uid)["native_session"], "fixture-session")

    def test_eligibility_is_metadata_only_and_execute_rechecks_native_route(self):
        with patch.object(worker, "route_available") as route:
            self.assertEqual(worker.eligible(self.config, self.uid), {"eligible":True})
            route.assert_not_called()
            route.return_value = ({}, "native_login_required")
            with patch.object(runner, "run") as run:
                answer = worker.execute_turn(self.config, self.uid, str(uuid.uuid4()))
                self.assertEqual(answer, {"disposition":"idle","reason":"native_login_required"})
                run.assert_not_called()

    def test_parked_task_and_malformed_configuration_on_real_port(self):
        self.store.park(self.uid, "sylan", question="Choose a revision")
        process = subprocess.run(worker._args("execute", self.uid), env=self.env, capture_output=True,
                                 input=b"", check=True, timeout=30, cwd=worker._HERE)
        self.assertEqual(json.loads(process.stdout), {"disposition":"waiting_human"})
        env = dict(self.env, FACTORY_STATE="relative.db")
        broken = subprocess.run(worker._args("eligible", self.uid), env=env, capture_output=True,
                                input=b"", timeout=30, cwd=worker._HERE)
        self.assertNotEqual(broken.returncode, 0)
        self.assertFalse(broken.stdout)

    def test_explicit_port_cancel_interrupts_without_consuming_another_run(self):
        process, actual = self.launch("guardian")
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
        process.wait(timeout=20)
        self.assertFalse(_alive(actual["leaf"]))
        self.assertTrue(self.store.needs_turn(self.uid))
        self.assertEqual(self.store.get(self.uid)["native_session"], "fixture-session")

    def test_pre_turn_route_backoff_is_durable_and_expires(self):
        def unavailable(_, task_id, token):
            return [sys.executable, str(Path(__file__).resolve()), "unavailable", task_id, token]
        with patch.dict(os.environ, self.env):
            result = worker.guard(self.config, self.uid, threading.Event(), command=unavailable)
        self.assertEqual(result["reason"], "native_login_required")
        self.assertFalse(worker.eligible(self.config, self.uid)["eligible"])
        self.assertTrue(self.store.needs_turn(self.uid))
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        with patch("factory.symphony_worker.time.time", return_value=time.time() + 61):
            self.assertTrue(worker.eligible(self.config, self.uid)["eligible"])

    def test_budgeted_runner_wakes_the_bound_session_and_parks_again(self):
        first = self.store.begin(self.uid, "sylan", "fixture", "low")
        self.store.bind(self.uid, first, "original")
        question = self.store.finish(self.uid, first, "waiting_human", question="Continue?")
        self.store.decide(question, "Continue local verification")
        token = str(uuid.uuid4())
        policy = planner.dispatch_resources(self.config.plan, "sylan", root=self.root)

        def native(store, task_id, root, profiles, model, effort, *, read_only, limits, instructions):
            self.assertEqual((root, profiles, model, effort, read_only, limits),
                (self.root, self.config.profiles, "fixture", "low", True, {"minutes":1, "tokens":12}))
            self.assertEqual(store.get(task_id)["native_session"], "original")
            self.assertEqual(store.begin(task_id, "sylan", model, effort), token)
            store.bind(task_id, token, "original")
            store.finish(task_id, token, "waiting_human", question="Another scope decision?")
            return {"run":token, "session":"original"}
        with patch.object(worker, "route_available", return_value=(policy, None)), patch.object(runner, "run", native):
            result = worker.execute_turn(self.config, self.uid, token)
        self.assertEqual(result["disposition"], "waiting_human")
        self.assertEqual(result["session"], "original")

    def test_closed_slot_and_unrelated_active_task_prevent_dispatch(self):
        config = worker.Config(self.root, self.config.state, self.config.profiles, self.config.plan, False)
        self.assertEqual(worker.eligible(config, self.uid), {"eligible":False})
        another = str(uuid.uuid4())
        self.store.discover(another, "NIN-2", "https://github.com/example/repo/issues/2", "other")
        self.store.claim(another, "matt", "rev", self.root / "other", "base")
        self.store.begin(another, "matt", "fixture", "low")
        self.assertEqual(worker.eligible(self.config, self.uid), {"eligible":False})


if __name__ == "__main__":
    _fixture(*sys.argv[1:])
