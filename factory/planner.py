"""Astra resource decisions. Running this module never claims or launches a line."""

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from .planner_transport import ResponsesPlanner
from . import runner
from .runtime import CodexRuntime, RuntimeUnavailable
from .state import Conflict, LINES, now, upstream_key

MODEL = "gpt-6.2-astra"
EFFORT = "ultra"
SLOTS = {"investigate": "read_only_investigation_slots", "implement": "implementation_slots"}
NAMES = dict(zip(("Sylan", "Matt Pocock", "Lauren Tan", "Addy Osmani"), LINES))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode(data):
    obj = json.loads(data, object_pairs_hook=unique_fields)
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object")
    return obj


def number(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("Expected an integer within the allowed resource range")
    return value


def read_seed(path):
    data = path.read_bytes()
    p = decode(data.decode("utf-8-sig"))
    if p.get("schema_version") != "1.0" or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", p["plan_id"]):
        raise ValueError("Unsupported plan schema or plan ID")
    intended = p["planner"]["requested"]
    if (intended["model"], intended["reasoning_effort"]) != (MODEL, EFFORT):
        raise ValueError("Seed requests a different planner")
    resources = p["capacity"]["now"]
    slots = {mode: number(resources[key]) for mode, key in SLOTS.items()}
    after_headroom = number(p["capacity"].get("after_headroom", {}).get("implementation_slots", 0))
    if max(*slots.values(), after_headroom) > 1:
        raise ValueError("The single-slot dispatcher supports at most one slot")
    candidates = {}
    for item in p["candidates"]:
        key = item["linear_id"]
        if key in candidates or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", key):
            raise ValueError("Invalid or repeated candidate identifier")
        candidates[key] = {"upstream_key": upstream_key(item["url"]), "scope": item["key"]}
    requests = {}
    for request in p["model_allocation"]["line_requests"]:
        for display in request["lines"]:
            line = NAMES[display]
            if line in requests:
                raise ValueError("Repeated line resource request")
            model, effort = request["model_request"], request["effort_request"]
            if not all(isinstance(s, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", s) for s in (model, effort)):
                raise ValueError("Invalid initial model or effort")
            requests[line] = {"model": model, "effort": effort}
    if set(requests) != set(LINES):
        raise ValueError("Missing line resource requests")
    budget = p["budget"]
    limits = {"investigate": budget["investigation_per_admitted_issue"],
              "implement": budget["one_implementation_attempt_after_admission"]}
    ceilings = {"investigate": [number(limits["investigate"]["wall_minutes"], 1),
                                number(limits["investigate"]["generated_tokens_review_at"], 1)],
                "implement": [number(limits["implement"]["wall_minutes_review_at"], 1),
                              number(limits["implement"]["cumulative_generated_tokens_including_native_delegates_review_at"], 1)]}
    return {"plan_id": p["plan_id"], "seed_sha256": digest(data), "slots": slots,
            "candidates": candidates, "requested": requests, "review_limits": ceilings,
            "implementation_slots_after_headroom": after_headroom}


def coordinator(root, seed):
    path = (root / "factory.db").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = list(db.execute("SELECT identifier, upstream_key, state, owner, revision, active_run FROM tasks"))
    ready = [r["identifier"] for r in rows if r["state"] == "ready" and r["owner"] is None
             and r["identifier"] in seed["candidates"]
             and seed["candidates"][r["identifier"]]["upstream_key"] == r["upstream_key"]]
    # The coordinator owns wake-up semantics. Dispatch rechecks needs_turn under its lock.
    owned = {line: [] for line in LINES}
    resumable = {line: [] for line in LINES}
    for row in rows:
        line = row["owner"]
        if line not in owned or row["state"] == "closed":
            continue
        owned[line].append(row["identifier"])
        candidate = seed["candidates"].get(row["identifier"])
        if (candidate and candidate["upstream_key"] == row["upstream_key"]
                and row["state"] in {"claimed", "ready_to_resume", "failed"}):
            resumable[line].append(row["revision"])
    return {"ready": sorted(ready), "active_runs": sum(bool(r["active_run"]) for r in rows),
            "owned": owned, "resumable_revisions": resumable}


def probe_lines(root, profiles=None):
    """Use the newest doctor discovery per line, then ask its pinned native host."""
    latest = {}
    for path in sorted((root / "probes").glob("*/discovery.json"),
                       key=lambda p: (p.stat().st_mtime_ns, str(p))):
        raw = path.read_bytes()
        obs = decode(raw)
        if obs["line"] in LINES:
            latest[obs["line"]] = (obs, digest(raw), path.parent)
    result = {line: {"authenticated": None, "models": {}} for line in LINES}
    for line, (obs, sha, cwd) in latest.items():
        revision = obs["revision"]
        length = 64 if line == "sylan" else 40
        if not isinstance(revision, str) or not re.fullmatch("[0-9a-f]{%d}" % length, revision):
            raise ValueError("Invalid profile revision")
        cap = result[line]
        cap.update(revision=revision, discovery_sha256=sha)
        try:
            _, home, plugin = runner.profile(line, Path(profiles) if profiles else root / "profiles",
                                             pinned=revision)
            with runner.host(line, home, cwd, plugin, read_only=True) as rt:
                live = rt.capabilities()
                combinations = live.get("models", {}) if line == "sylan" else rt.discover().get("model_efforts", {})
            cap.update(authenticated=live.get("authenticated"), models=combinations)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            cap["probe_status"] = "unavailable"
    return result


def history(directory, plan_id, expected):
    number(expected)
    previous = None
    for index in range(1, expected + 1):
        path = directory / f"{index:08d}.json"
        if not path.exists():
            raise Conflict("Expected revision does not exist")
        receipt = decode(path.read_bytes())
        payload = receipt["payload"]
        actual = digest(runner.json_bytes(payload))
        if (receipt["sha256"] != actual or payload["plan_id"] != plan_id
                or payload["revision"] != index or payload["parent_sha256"] != previous):
            raise Conflict("Broken decision history")
        previous = actual
    if (directory / f"{expected + 1:08d}.json").exists():
        raise Conflict("Stale expected revision")
    return previous


def validate(decision, context):
    """An offer is one capacity reservation proposal. The native line chooses an issue."""
    common = {"revision", "action", "reason"}
    action = decision.get("action")
    offer = {"line", "profile_revision", "mode", "model", "effort", "minutes", "tokens"}
    if action not in ("hold", "offer") or set(decision) != (common if action == "hold" else common | offer):
        raise ValueError("Invalid decision structure")
    if number(decision["revision"], 1) != context["next_revision"]:
        raise Conflict("Wrong model-response revision")
    reason = decision["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        raise ValueError("Invalid decision reason")
    if action == "hold":
        return
    line, mode = decision["line"], decision["mode"]
    if line not in LINES or mode not in SLOTS:
        raise ValueError("Unknown line or execution mode")
    cap = context["lines"][line]
    if (cap["authenticated"] is not True or not cap.get("revision")
            or decision["profile_revision"] != cap["revision"]
            or decision["effort"] not in cap["models"].get(decision["model"], [])):
        raise ValueError("Line lacks verified revision, authentication, model or effort")
    pool = context["coordinator"]
    slots = context["seed"]["slots"][mode]
    if mode == "implement":
        peak = context["estimated_peak_bytes"]
        if peak is None or context["free_bytes"] < number(peak, 1) + 4 * 1024**3:
            raise ValueError("Insufficient verified disk headroom")
        if context["next_revision"] > 1 and trigger_category(context["trigger"]) == "capacity":
            slots = max(slots, context["seed"]["implementation_slots_after_headroom"])
    eligible = (decision["profile_revision"] in pool["resumable_revisions"][line]
                or (not pool["owned"][line] and pool["ready"]))
    if not eligible or pool["active_runs"] >= slots:
        raise ValueError("No available slot or eligible candidate")
    minutes, tokens = context["seed"]["review_limits"][mode]
    if number(decision["minutes"], 1) > minutes or number(decision["tokens"], 1) > tokens:
        raise ValueError("Decision exceeds the seed review budget")


def dispatch_resources(plan_path, line, *, read_only=True, root=None):
    """Return the latest shared slot policy for one line. Enforce it at claim/run time.

    Always pass the seed plan. Revision zero is its initial policy. Subsequent
    decisions are discovered in ROOT/planner/PLAN_ID; ROOT defaults to the
    seed's directory. A hold closes both modes on every line. Call again inside
    the dispatcher's admission lock, immediately before granting a slot.
    """
    if line not in LINES or type(read_only) is not bool:
        raise ValueError("Invalid dispatch line or mode")
    path = Path(plan_path).resolve()
    seed = read_seed(path)
    directory = (Path(root) if root is not None else path.parent) / "planner" / seed["plan_id"]
    revisions = [int(p.stem) for p in directory.glob("*.json")
                 if re.fullmatch(r"[0-9]{8}", p.stem)]
    mode = "investigate" if read_only else "implement"
    minutes, tokens = seed["review_limits"][mode]
    result = {"minutes": minutes if seed["slots"][mode] else 0,
              "tokens": tokens if seed["slots"][mode] else 0, "slots": seed["slots"][mode], **seed["requested"][line],
              "revision": 0, "profile_revision": None, "sha256": seed["seed_sha256"]}
    if not revisions:
        return result
    revision = max(revisions)
    receipt_hash = history(directory, seed["plan_id"], revision)
    receipt = decode((directory / f"{revision:08d}.json").read_bytes())
    payload = receipt["payload"]
    if payload.get("transport") == "responses":
        completion = payload.get("provider_completion")
        if not payload.get("request_id") or not payload.get("response_id"):
            raise Conflict("Missing Responses identifiers")
    elif "transport" not in payload and payload.get("thread_id") and payload.get("turn_id"):
        completion = payload.get("turn")  # Receipts produced by earlier native implementations.
    else:
        raise Conflict("Unrecognized decision transport")
    if (payload["seed_sha256"] != seed["seed_sha256"] or receipt["sha256"] != receipt_hash
            or digest(runner.json_bytes(payload)) != receipt_hash
            or completion != {"model": MODEL, "effort": EFFORT, "status": "completed"}):
        raise Conflict("Decision does not match the current seed or verified planner turn")
    decision = payload["decision"]
    context = payload["context"]
    if context["seed"] != seed or context["next_revision"] != revision:
        raise Conflict("Decision context disagrees with seed or revision")
    validate(decision, context)
    result.update(slots=0, minutes=0, tokens=0, revision=revision, sha256=receipt_hash)
    if decision["action"] == "offer" and decision["line"] == line and decision["mode"] == mode:
        if mode == "implement":
            peak = number(payload["context"]["estimated_peak_bytes"], 1)
            if shutil.disk_usage(directory).free < peak + 4 * 1024**3:
                return result
        result.update(slots=1, minutes=decision["minutes"], tokens=decision["tokens"], model=decision["model"], effort=decision["effort"],
                      profile_revision=decision["profile_revision"])
    return result


def trigger_category(value):
    categories = {"initial-capacity-check": "initial", "disk-headroom-changed": "capacity",
                  "capacity check": "capacity", "quota-changed": "budget",
                  "pool-changed": "queue", "profile-changed": "capability"}
    return categories.get(value, "other")


def model_view(ctx):
    """Build the only application data passed to run(). Keep route identities local."""
    seed, queue = ctx["seed"], ctx["coordinator"]
    routes, choices = [], []
    for line in LINES:
        cap = ctx["lines"].get(line, {})
        if cap.get("authenticated") is not True or not cap.get("revision"):
            continue
        for model, efforts in sorted(cap.get("models", {}).items()):
            for effort in sorted(set(efforts)):
                choice = len(routes)
                routes.append({"line": line, "model": model, "effort": effort,
                               "profile_revision": cap["revision"]})
                choices.append({"option": choice, "lane": LINES.index(line),
                                "preferred": seed["requested"][line] == {"model": model, "effort": effort},
                                "effort": effort if effort in ("low","medium","high","xhigh","max","ultra") else "other",
                                "owned": len(queue["owned"][line]),
                                "resumable": queue["resumable_revisions"][line].count(cap["revision"])})
    mode_limits = dict(seed["slots"])
    peak = ctx["estimated_peak_bytes"]
    if peak is None or ctx["free_bytes"] < peak + 4 * 1024**3:
        mode_limits["implement"] = 0
    elif ctx["next_revision"] > 1 and trigger_category(ctx["trigger"]) == "capacity":
        mode_limits["implement"] = max(mode_limits["implement"], seed["implementation_slots_after_headroom"])
    return {"revision": ctx["next_revision"], "event": trigger_category(ctx["trigger"]),
            "ready": len(queue["ready"]), "active": queue["active_runs"], "slots": mode_limits,
            "limits_minutes_tokens": seed["review_limits"], "options": choices}, routes


def local_decision(text, routes):
    answer = decode(text)
    if answer.get("action") == "offer":
        if set(answer) != {"action","revision","reason","option","mode","minutes","tokens"}:
            raise ValueError("Invalid anonymous offer")
        option = number(answer.pop("option"))
        if option >= len(routes):
            raise ValueError("Unknown route option")
        answer.update(routes[option])
    return answer


GUIDANCE = """Decide resource allocation using only the supplied counts, limits and verified options.
Return JSON with revision, action (hold or offer), and a brief reason. Only an offer also has
option (integer), mode (investigate or implement), minutes and tokens. Respect the shared slots,
existing ownership and limits. Prefer a preferred option when suitable. Do not use tools."""


def planner_capabilities(rt):
    caps = rt.capabilities()
    return {"authenticated": caps.get("authenticated") is True,
            "advertised": EFFORT in caps.get("models", {}).get(MODEL, [])}


def check_planner(profile):
    """Read only the planner host's model catalogue and authentication flags."""
    with tempfile.TemporaryDirectory() as cwd:
        with CodexRuntime(profile, cwd, read_only=True) as rt:
            return planner_capabilities(rt)


def decide(root, plan_path, expected_revision, trigger, *, transport, estimated_peak_bytes=None, profiles=None):
    if transport != "responses":
        raise ValueError("Explicit Responses transport required")
    root, plan_path = Path(root).resolve(), Path(plan_path).resolve()
    seed = read_seed(plan_path)
    directory = root / "planner" / seed["plan_id"]
    parent = history(directory, seed["plan_id"], expected_revision)
    if not isinstance(trigger, str) or not trigger.strip() or len(trigger) > 512:
        raise ValueError("A short event or initial scheduling reason is required")
    if estimated_peak_bytes is not None:
        number(estimated_peak_bytes, 1)
    attempt = str(uuid.uuid4())
    cwd = directory / "attempts" / attempt
    cwd.mkdir(parents=True, exist_ok=False)
    evidence = {"plan_id": seed["plan_id"], "seed_sha256": seed["seed_sha256"],
                "revision": expected_revision + 1, "parent_sha256": parent,
                "attempt": attempt, "started_at": now(), "trigger": trigger,
                "requested_model": MODEL, "requested_effort": EFFORT, "transport": transport}
    try:
        client = ResponsesPlanner()
        evidence["credentials_configured"] = client.credentials_configured
        if not client.credentials_configured:
            raise RuntimeUnavailable("Dedicated Responses API credential required")
        context = {"seed": seed, "next_revision": expected_revision + 1, "trigger": trigger,
                   "previous": None if not parent else decode(
                       (directory / f"{expected_revision:08d}.json").read_bytes())["payload"]["decision"],
                   "lines": probe_lines(root, profiles), "coordinator": coordinator(root, seed),
                   "free_bytes": shutil.disk_usage(root).free, "estimated_peak_bytes": estimated_peak_bytes}
        evidence["context"] = context
        events = []
        def observe(name, attrs):
            if name == "runtime.turn":
                events.append({key: attrs.get(key) for key in ("model", "effort", "status")})
        public, routes = model_view(context)
        result = client.run_data_only(GUIDANCE + "\n" + json.dumps(public), MODEL, EFFORT, on_event=observe)
        if (not result.request_id or not result.response_id or
                events != [{"model": MODEL, "effort": EFFORT, "status": "completed"}]):
            raise RuntimeUnavailable("Missing completed Astra Ultra provider evidence")
        evidence.update(request_id=result.request_id, response_id=result.response_id,
                        provider_completion=events[0], response_sha256=digest(result.text.encode("utf-8")))
        decision = local_decision(result.text, routes)
        validate(decision, context)
        if digest(plan_path.read_bytes()) != seed["seed_sha256"]:
            raise Conflict("Seed changed during the turn")
        if decision["action"] == "offer":
            fresh = {**context, "coordinator": coordinator(root, seed), "lines": probe_lines(root, profiles),
                     "free_bytes": shutil.disk_usage(root).free}
            validate(decision, fresh)
            evidence["admission_readback"] = {k: fresh[k] for k in ("coordinator", "lines", "free_bytes")}
        evidence.update(decision=decision, completed_at=now())
        receipt = {"payload": evidence, "sha256": digest(runner.json_bytes(evidence))}
        target = directory / f"{expected_revision + 1:08d}.json"
        runner.write_once(target, runner.json_bytes(receipt))
        return {"revision": expected_revision + 1, "artifact": str(target), "sha256": receipt["sha256"],
                "decision": decision}
    except Exception as exc:
        # Never retain raw host errors, transcripts or invalid model output.
        failure = {**evidence, "status": "failed", "error_type": type(exc).__name__, "failed_at": now()}
        failure.pop("decision", None)
        target = cwd / "failure.json"
        runner.write_once(target, runner.json_bytes(failure))
        exc.add_note("Failure receipt: " + str(target))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".factory-local"), help="Factory state and evidence")
    parser.add_argument("--plan", type=Path, help="Default: ROOT/model-plan.json")
    parser.add_argument("--transport", choices=["responses"], help="Required for a model decision")
    parser.add_argument("--profile", type=Path, help="Dedicated authenticated Codex planner profile")
    parser.add_argument("--expected-revision", type=int, help="0 for initial decision")
    parser.add_argument("--profiles", type=Path, help="Existing production-line profiles (default ROOT/profiles)")
    parser.add_argument("--check-planner", action="store_true", help="Check auth and model support without a model turn or factory data")
    parser.add_argument("--trigger", help="Scheduling or replanning event")
    parser.add_argument("--estimated-peak-bytes", type=int, help="Incremental peak on factory volume")
    args = parser.parse_args(argv)
    try:
        if args.check_planner:
            if args.transport:
                parser.error("--check-planner checks only the native profile")
            cap = check_planner(args.profile or args.root / "planner-profile")
            print(json.dumps({**cap, "model": MODEL, "effort": EFFORT, "turn_started": False}))
            return 0 if all(cap.values()) else 2
        if not args.transport or args.expected_revision is None or not args.trigger:
            parser.error("--transport responses, --expected-revision and --trigger are required for decisions")
        if args.profile:
            parser.error("--profile is only used with --check-planner")
        result = decide(args.root, args.plan or args.root / "model-plan.json", args.expected_revision,
                        args.trigger, transport=args.transport, estimated_peak_bytes=args.estimated_peak_bytes,
                        profiles=args.profiles)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        print(f"Planner failed ({type(error).__name__}).", file=sys.stderr)
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
