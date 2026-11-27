"""Native Symphony Port backend. stdout carries exactly one JSON receipt."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from . import runner
from .dispatch import available_slot, requested_model_allowed, route_available
from .planner import dispatch_resources
from .runtime import RuntimeUnavailable
from .state import Conflict, LINES, Store
from .worker_process import ProcessTree, orphan_gone, port_cancelled, wait_for_start


_HERE = Path(__file__).resolve().parents[1]
_DISPOSITIONS = {"waiting_human", "waiting_upstream", "idle", "failed"}


@dataclass(frozen=True)
class Config:
    root: Path
    state: Path
    profiles: Path
    plan: Path
    read_only: bool

    @classmethod
    def load(cls):
        paths = []
        for key, directory in (("ROOT", True), ("STATE", False), ("PROFILES", True), ("PLAN", False)):
            path = Path(os.environ.get("FACTORY_" + key, ""))
            if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
                raise ValueError(f"FACTORY_{key} must point to an existing absolute path")
            paths.append(path.resolve(strict=True))
        mode = os.environ.get("FACTORY_MODE", "investigate")
        if mode not in {"investigate", "implement"}:
            raise ValueError("FACTORY_MODE must be investigate or implement")
        return cls(*paths, read_only=mode == "investigate")


class _RunStore(Store):
    def __init__(self, path, owned_run):
        self.owned_run = owned_run
        super().__init__(path)

    def begin(self, task_id, line, model, effort, trace_id=None):
        return super().begin(task_id, line, model, effort, trace_id,
                             run_id=self.owned_run, require_wakeup=True)


def _receipt(disposition, reason=None, **fields):
    data = {"disposition": disposition, **fields}
    if reason:
        data["reason"] = reason
    return data


def _check(config, store, task_id):
    try:
        task = store.get(task_id)
    except ValueError:
        return None, _receipt("idle", "unknown_task")
    if task["state"] in {"waiting_human", "waiting_upstream"}:
        return None, _receipt(task["state"])
    if task["owner"] not in LINES or not store.needs_turn(task_id):
        return None, _receipt("idle", "no_pending_turn")
    if any(row["active_run"] for row in store.pool()):
        return None, _receipt("idle", "occupied")
    worktree = config.root / "worktrees" / task_id
    if (not task["revision"] or not task["base"] or not worktree.is_dir()
            or Path(task["worktree"]).resolve() != worktree or worktree.resolve() != worktree):
        return None, _receipt("failed", "invalid_claim")
    revision = task["revision"]
    if task["owner"] == "sylan":
        pinned = config.profiles / "sylan" / "revisions" / revision / "AGENTS.md"
        valid_pin = re.fullmatch(r"[0-9a-f]{64}", revision) and pinned.is_file()
    else:
        pinned = config.profiles / task["owner"] / "native" / task["owner"] / revision / "plugin"
        valid_pin = re.fullmatch(r"[0-9a-f]{40}", revision) and pinned.is_dir()
    if not valid_pin:
        return None, _receipt("failed", "pinned_profile_missing")
    plan = dispatch_resources(config.plan, task["owner"], root=config.root, read_only=config.read_only)
    if plan["slots"] < 1 or plan["profile_revision"] not in (None, task["revision"]):
        return None, _receipt("idle", "no_matching_planner_slot")
    if not requested_model_allowed(plan["model"]):
        return None, _receipt("idle", "model_replan_required")
    cooldown = _cooldown(config, task_id)
    if cooldown.exists():
        record = json.loads(cooldown.read_bytes())
        if (record["policy"] == plan["sha256"] and record["revision"] == revision
                and record["line"] == task["owner"] and time.time() < record["until"]):
            return None, _receipt("idle", "native_route_backoff")
    return (task, plan), None


def _marker(config, task_id):
    return config.root / "symphony-workers" / (task_id + ".json")


def _cooldown(config, task_id):
    return config.root / "symphony-workers" / "backoff" / (task_id + ".json")


def _reconcile(config, store, task_id):
    path = _marker(config, task_id)
    if not path.exists():
        return True
    data = json.loads(path.read_bytes())
    token = str(uuid.UUID(data["run"]))
    pid = data["pid"]
    if (data["task"] != task_id or data["state_db"] != str(config.state)
            or type(pid) is not int or pid < 1):
        raise ValueError("Invalid Symphony ownership receipt")
    if not orphan_gone(token, pid):
        return False
    _recover_owned(store, task_id, token)
    path.unlink()
    return True


def _reconcile_all(config, store):
    directory = config.root / "symphony-workers"
    return all(_reconcile(config, store, str(uuid.UUID(path.stem))) for path in directory.glob("*.json"))


def _recover_owned(store, task_id, token):
    try:
        store.recover(task_id, token)
    except (ValueError, Conflict):
        # Nothing claimed yet, a completed/superseded run, or another owner's new run.
        pass


def eligible(config, task_id):
    with available_slot(config.state.with_suffix(".dispatch.lock")) as acquired:
        if not acquired:
            return {"eligible": False}
        store = Store(config.state)
        if not _reconcile_all(config, store):
            return {"eligible": False}
        ready, _ = _check(config, store, task_id)
        return {"eligible": ready is not None}


def execute_turn(config, task_id, token):
    store = _RunStore(config.state, token)
    ready, result = _check(config, store, task_id)
    if not ready:
        return result
    task, offer = ready
    line = task["owner"]
    try:
        policy, why = route_available(config.root, config.profiles, config.plan, line, config.read_only,
                                      pinned=task["revision"])
    except (RuntimeUnavailable, TimeoutError, FileNotFoundError) as error:
        return _receipt("failed", type(error).__name__)
    if why or policy["sha256"] != offer["sha256"]:
        return _receipt("idle", why or "plan_changed")
    if not store.needs_turn(task_id):
        return _receipt("idle", "wake_consumed")
    latest = dispatch_resources(config.plan, line, root=config.root, read_only=config.read_only)
    if latest["sha256"] != policy["sha256"] or latest["slots"] < 1:
        return _receipt("idle", "plan_changed")
    try:
        result = runner.run(store, task_id, config.root, config.profiles, policy["model"], policy["effort"],
                            read_only=config.read_only,
                            limits={"minutes": policy["minutes"], "tokens": policy["tokens"]},
                            instructions=("Investigate within the selected read-only scope and record findings."
                                          if config.read_only else "Advance the authorized local contribution."))
    except Conflict:
        state = store.get(task_id)["state"]
        if state == "failed" or (state == "claimed" and store.needs_turn(task_id)):
            return _receipt("failed", "run_conflict", run=token)
        return _receipt(state if state in {"waiting_human", "waiting_upstream"} else "idle", "superseded")
    except (RuntimeError, OSError, ValueError) as exc:
        return _receipt("failed", type(exc).__name__, run=token)
    state = store.get(task_id)["state"]
    return _receipt(state if state in {"waiting_human", "waiting_upstream"} else "idle",
                    run=result["run"], session=result.get("session"), trace_id=result.get("trace_id"))


def _args(action, task_id, *extra):
    return [sys.executable, "-m", "factory.symphony_worker", action, task_id, *extra]


def _read_receipt(output):
    output.seek(0)
    data = output.read(65537)
    if len(data) > 65536:
        raise RuntimeError("Unexpected worker output size")
    receipt = json.loads(data)
    if not isinstance(receipt, dict) or receipt.get("disposition") not in _DISPOSITIONS:
        raise RuntimeError("Invalid worker disposition")
    return receipt


def guard(config, task_id, cancelled, *, command=_args):
    """Only this process holds the execution slot and the descendant lifetime handle."""
    with available_slot(config.state.with_suffix(".dispatch.lock")) as acquired:
        if acquired:
            store = Store(config.state)
            if not _reconcile_all(config, store):
                return _receipt("idle", "previous_tree_terminating")
            ready, result = _check(config, store, task_id)
            if not ready:
                return result
            if cancelled.is_set():
                return _receipt("failed", "cancelled")
            token = str(uuid.uuid4())
            marker = _marker(config, task_id)
            with tempfile.TemporaryFile() as output:
                tree = ProcessTree(command("_turn", task_id, token), token, output, cwd=_HERE)
                try:
                    runner.write_once(marker, runner.json_bytes(
                        {"task": task_id, "run": token, "pid": tree.process.pid, "state_db": str(config.state)}))
                    if not cancelled.is_set():
                        tree.start()
                    while tree.process.poll() is None and not cancelled.wait(0.05):
                        pass
                    finished = tree.process.poll()
                finally:
                    tree.close()
                # Hard cancellation, an unexpected child exit, and normal completion all
                # use the same exact compare-and-set. Terminal transitions win the race.
                _recover_owned(store, task_id, token)
                marker.unlink()
                if finished is None:
                    return _receipt("failed", "cancelled", run=token)
                if finished != 0:
                    raise RuntimeError("Native turn process exited without a valid receipt")
                result = _read_receipt(output)
                if "run" not in result and result.get("reason") in {
                        "native_login_required", "native_profile_unavailable", "native_host_unavailable",
                        "model_replan_required", "RuntimeUnavailable",
                        "TimeoutError", "FileNotFoundError"}:
                    task, policy = ready
                    cooldown = _cooldown(config, task_id)
                    cooldown.parent.mkdir(parents=True, exist_ok=True)
                    pending = cooldown.with_suffix("." + token + ".tmp")
                    pending.write_bytes(runner.json_bytes({
                        "line":task["owner"], "revision":task["revision"], "policy":policy["sha256"],
                        "until":time.time() + 60, "reason":result["reason"]}))
                    os.replace(pending, cooldown)
                return result
        return _receipt("idle", "occupied")


def relay(task_id, cancelled):
    """The guardian survives a hard Port-child exit and observes EOF on its own pipe."""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(_args("_guard", task_id), cwd=_HERE, stdin=subprocess.PIPE,
                                   stdout=output, stderr=subprocess.DEVNULL, close_fds=True,
                                   start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                if cancelled.is_set() and not process.stdin.closed:
                    process.stdin.close()
                time.sleep(0.05)
            if process.returncode:
                raise RuntimeError("Symphony worker did not produce a valid receipt")
            return _read_receipt(output)
        finally:
            if not process.stdin.closed:
                process.stdin.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("eligible", "execute", "_guard", "_turn"))
    parser.add_argument("task")
    parser.add_argument("token", nargs="?")
    args = parser.parse_args(argv)
    task_id = str(uuid.UUID(args.task))
    if args.action == "_turn" and not wait_for_start():
        return 1
    config = Config.load()
    if args.action == "eligible":
        result = eligible(config, task_id)
    elif args.action == "execute":
        result = relay(task_id, port_cancelled())
    elif args.action == "_guard":
        result = guard(config, task_id, port_cancelled())
    else:
        token = str(uuid.UUID(args.token))
        owner = json.loads(_marker(config, task_id).read_bytes())
        if owner != {"task":task_id, "run":token, "pid":os.getpid(), "state_db":str(config.state)}:
            raise ValueError("Executor does not own this worker receipt")
        result = execute_turn(config, task_id, token)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("Symphony worker infrastructure error: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
