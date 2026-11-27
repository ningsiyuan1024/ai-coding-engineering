"""Admit work from the shared pool for Symphony's supervised native workers."""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import runner
from .planner import dispatch_resources
from .runtime import RuntimeUnavailable
from .state import Conflict, LINES, Store


@contextmanager
def admission(path):
    """An OS lock is released even when the dispatcher process is terminated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Conflict("Another dispatcher has the pilot slot") from exc
        yield


@contextmanager
def available_slot(path):
    with ExitStack() as stack:
        try:
            stack.enter_context(admission(path))
        except Conflict:
            yield False
            return
        yield True


def requested_model_allowed(model):
    return (isinstance(model, str) and bool(model) and not re.search(
        r"(?:^|[/_.:-])(?:anthropic|claude|sonnet|opus|haiku|fable|auto|default)(?:$|[/_.:\[\]-])",
        model, flags=re.I))


def route_available(root, profiles, plan, line, read_only, *, pinned=None):
    policy = dispatch_resources(plan, line, root=root, read_only=read_only)
    if policy["slots"] < 1:
        return policy, "no_planner_slot"
    if not requested_model_allowed(policy["model"]):
        return policy, "model_replan_required"
    try:
        revision, home, plugin = runner.profile(line, profiles, pinned=pinned)
    except (OSError, ValueError, RuntimeUnavailable, subprocess.SubprocessError):
        return policy, "native_profile_unavailable"
    policy["installed_revision"] = revision
    if policy["profile_revision"] not in (None, revision):
        return policy, "profile_revision_changed"
    probe = root / "admission" / line
    probe.mkdir(parents=True, exist_ok=True)
    try:
        with runner.host(line, home, probe, plugin, read_only=True) as host:
            caps = host.capabilities()
            if not caps["authenticated"]:
                return policy, "native_login_required"
            model_efforts = caps.get("models")
            levels = model_efforts.get(policy["model"]) if isinstance(model_efforts, dict) else None
            if not isinstance(levels, (list, tuple)) or policy["effort"] not in levels:
                return policy, "model_replan_required"
    except (OSError, ValueError, RuntimeUnavailable, subprocess.SubprocessError):
        # A missing profile credential or local CLI is specific to this production line.
        # The planner decision was verified above, outside this error boundary.
        return policy, "native_host_unavailable"
    return policy, None


def tick(store, root, profiles, plan, sources, *, read_only=True, eligible_ids=None, refresh_scope=None):
    """Register a native line's selection. Symphony owns execution and monitoring."""
    from .eligibility import read_candidate

    root, profiles = Path(root).resolve(), Path(profiles).resolve()
    with available_slot(store.path.with_suffix(".dispatch.lock")) as acquired:
        if not acquired:
            return {"state": "occupied"}
        pool = store.pool()
        if any(t["active_run"] for t in pool):
            return {"state": "occupied"}
        ready = [t for t in pool if t["state"] == "ready" and
                 (eligible_ids is None or t["id"] in eligible_ids) and
                 t["upstream_key"].rsplit("/", 1)[0] in sources]
        pending = [t for t in pool if t["owner"] and store.needs_turn(t["id"])]
        reasons = {}
        evidence = None
        # Serve already claimed work before admitting a new investigation.
        order = list(dict.fromkeys([t["owner"] for t in pending] + list(LINES)))
        for line in order:
            task = next((t for t in pending if t["owner"] == line), None)
            if task and not Path(task["worktree"]).is_dir():
                source = sources.get(task["upstream_key"].rsplit("/", 1)[0])
                if not source:
                    reasons[line] = "worktree_source_required"
                    continue
                task = runner.claim(store, task["id"], line, source["path"], task["base"], root, profiles,
                                    selected_revision=task["revision"])
            if task and eligible_ids is not None and task["id"] not in eligible_ids:
                # Linear intake watches Backlog. The native worker independently reconciles
                # its broader active states and owns all follow-up work for this claim.
                reasons[line] = "owned_outside_intake"
                continue
            if not task and any(t["owner"] == line and t["state"] != "closed" for t in pool):
                reasons[line] = "pilot_already_owned"
                continue
            if not task and not ready:
                reasons[line] = "no_staged_candidate"
                continue
            policy, why = route_available(root, profiles, plan, line, read_only,
                                          pinned=task["revision"] if task else None)
            if why:
                reasons[line] = why
                continue
            if task is None:
                if evidence is None:
                    snapshots = {row["id"]: read_candidate(row) for row in ready}
                    evidence = {key: value for key, value in snapshots.items() if not value["blocked"]}
                if not evidence:
                    return {"state": "no_eligible_candidate"}
                # An unchanged pool a line declined should not generate a paid scout turn every poll.
                fingerprint = hashlib.sha256(runner.json_bytes([
                    policy["sha256"], policy["installed_revision"],
                    sorted((key, val["revision"]) for key, val in evidence.items())
                ])).hexdigest()
                declined = root / "selection" / line / "declined" / fingerprint
                if declined.is_file():
                    reasons[line] = "declined"
                    continue
                selected = runner.choose(store, line, profiles, root, policy["model"], policy["effort"],
                                         evidence=evidence, pinned=policy["installed_revision"])
                if selected is None:
                    runner.write_once(declined, b"NONE\n")
                    reasons[line] = "declined"
                    continue
                task = store.get(selected)
                live = read_candidate(task)
                if live["blocked"] or live["revision"] != evidence[selected]["revision"]:
                    return {"state": "upstream_changed", "task": selected}
                if refresh_scope is not None and selected not in refresh_scope():
                    return {"state": "linear_scope_changed", "task": selected}
                current_policy = dispatch_resources(plan, line, root=root, read_only=read_only)
                if current_policy["sha256"] != policy["sha256"] or not current_policy["slots"]:
                    return {"state": "plan_changed", "task": selected}
                source = sources[task["upstream_key"].rsplit("/", 1)[0]]
                task = runner.claim(store, selected, line, source["path"], source["base"], root, profiles,
                                    selected_revision=policy["installed_revision"])
            return {"state": "awaiting_symphony", "line": line, "task": task["id"],
                    "revision": task["revision"], "planner_revision": policy["revision"]}
        return {"state": "waiting", "lines": reasons}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(".factory-local"))
    p.add_argument("--state", type=Path)
    p.add_argument("--profiles", type=Path)
    p.add_argument("--plan", type=Path)
    p.add_argument("--sources", type=Path, help="Map owner/repo to a local path and pinned base")
    p.add_argument("--local-write", action="store_true")
    p.add_argument("--watch", action="store_true", help="Poll until interrupted")
    p.add_argument("--observe-only", action="store_true", help="Reconcile state and maintain pins without admitting model turns")
    p.add_argument("--maintain", action="store_true", help="Also perform due PR checks and skill updates on this single poll")
    p.add_argument("--receipt", type=Path, help="Write a private atomic receipt for this admission pass")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--symphony-root", type=Path, help="Installed Symphony elixir directory for live intake")
    args = p.parse_args(argv)
    if args.interval < 1:
        p.error("--interval must be positive")
    root = args.root.resolve()
    if args.watch:
        with admission(root / "dispatcher-service.lock"):
            return poll(args, root)
    return poll(args, root)


def linear_scope(root, symphony_root, store):
    if symphony_root:
        if not (os.environ.get("OSS_LINEAR_API_KEY") or os.environ.get("LINEAR_API_KEY")):
            return set(), "LinearAuthError"
        from .linear import LinearError, sync_symphony
        try:
            result = sync_symphony(symphony_root, store.path.resolve())
            return set(result["eligible_ids"]), "symphony_tracker"
        except LinearError as exc:
            return set(), type(exc).__name__
    receipt = root / "linear-readback.json"
    if receipt.is_file():
        snapshot = json.loads(receipt.read_text(encoding="utf-8-sig"))
        age = time.time() - datetime.fromisoformat(snapshot["observed_at"]).timestamp()
        if 0 <= age <= 600:
            return set(snapshot["eligible_ids"]), "linear_connector_snapshot"
    return set(), "fresh_linear_snapshot_required"


def poll(args, root):
    store = Store(args.state or root / "factory.db")
    profiles = args.profiles or root / "profiles"
    plan = args.plan or root / "model-plan.json"
    config = (args.sources or root / "sources.json").resolve()
    previous = None
    while True:
        try:
            sources = {} if args.observe_only else json.loads(config.read_text(encoding="utf-8-sig"))
            for entry in sources.values():
                entry["path"] = (config.parent / entry["path"]).resolve()
            scope, intake_status = linear_scope(root, args.symphony_root, store)
            if args.observe_only:
                result = {"state": "observing", "pool_size": len(store.pool()), "eligible_now": len(scope)}
            else:
                result = tick(store, root, profiles, plan, sources, read_only=not args.local_write,
                              eligible_ids=scope, refresh_scope=lambda: linear_scope(root, args.symphony_root, store)[0])
            result["intake"] = intake_status
            if args.watch or args.maintain:
                from .maintenance import maintain
                result["maintenance"] = maintain(store, root, profiles)
        except (OSError, ValueError, RuntimeError) as exc:
            if not args.watch:
                raise
            result = {"state": "error", "reason": str(exc)}
        if result != previous:
            print(json.dumps(result, ensure_ascii=True), flush=True)
            previous = result
        heartbeat = args.receipt or (root / "dispatcher-status.json" if args.watch else None)
        if heartbeat:
            update = heartbeat.with_suffix(f".{os.getpid()}.tmp")
            data = {"pid": os.getpid(), "checked_at": datetime.now(timezone.utc).isoformat(), "result": result}
            if args.watch:
                data["interval_seconds"] = args.interval
            if os.environ.get("FACTORY_SYMPHONY_PID"):
                data["symphony_pid"] = os.environ["FACTORY_SYMPHONY_PID"]
            update.write_bytes(runner.json_bytes(data))
            os.replace(update, heartbeat)
        if not args.watch:
            return 0
        stop_request = root / f"stop-{os.getpid()}.request"
        if stop_request.exists():
            stop_request.unlink()
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
