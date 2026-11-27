"""Run `python -m factory --help` from the factory checkout."""

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import runner
from .state import Conflict, LINES, Store
from .telemetry import LocalTraceReceiver, Trace, read_logs, read_metrics, read_trace


def parser():
    p = argparse.ArgumentParser(description="AI Software Factory coordinator")
    p.add_argument("--root", type=Path, default=Path(".factory-local"), help="Private run and worktree directory")
    p.add_argument("--state", type=Path, help="Single shared coordinator database (default ROOT/factory.db)")
    p.add_argument("--profiles", type=Path, help="Four isolated line homes (default ROOT/profiles)")
    cmd = p.add_subparsers(dest="command", required=True)
    pull = cmd.add_parser("import-pool", help="Ingest read-back Linear records")
    pull.add_argument("file", type=Path)
    cmd.add_parser("pool")
    cmd.add_parser("inbox")
    cmd.add_parser("status", help="Show all lines, runs, and pending human decisions together")
    wake = cmd.add_parser("request-turn", help="Schedule the owning line for another turn")
    wake.add_argument("task")
    wake.add_argument("reason")
    recover = cmd.add_parser("recover", help="Release a confirmed stopped host's exact in-flight run")
    recover.add_argument("task")
    recover.add_argument("stopped_run")
    doc = cmd.add_parser("doctor", help="Probe installed runtime, auth, native skill discovery and OTLP")
    doc.add_argument("line",choices=LINES)
    login = cmd.add_parser("login", help="Open the provider's own login in an isolated line profile")
    login.add_argument("line", choices=LINES)
    chk = cmd.add_parser("sync-skills", help="Check upstream and install a revision for future claims")
    chk.add_argument("line",choices=LINES[1:])
    take = cmd.add_parser("claim", help="Reserve work chosen by a line at a pinned baseline")
    take.add_argument("line",choices=LINES)
    take.add_argument("task",help="Linear UUID")
    take.add_argument("repo",type=Path)
    take.add_argument("--base",default="HEAD")
    pick = cmd.add_parser("choose",help="Ask a native line which eligible task it wants")
    pick.add_argument("line",choices=LINES)
    invoke = cmd.add_parser("run",help="Start or resume the original native session")
    invoke.add_argument("task")
    invoke.add_argument("--instructions", required=True)
    invoke.add_argument("--local-write",action="store_true",help="Allow the native host to edit its task worktree")
    for c in (pick,invoke):
        c.add_argument("--model",required=True)
        c.add_argument("--effort",required=True)
    human = cmd.add_parser("ask")
    human.add_argument("task")
    human.add_argument("line",choices=LINES)
    human.add_argument("question")
    ans = cmd.add_parser("answer")
    ans.add_argument("decision")
    ans.add_argument("text")
    pr = cmd.add_parser("track-pr")
    pr.add_argument("task")
    pr.add_argument("line",choices=LINES)
    pr.add_argument("url")
    review = cmd.add_parser("pr-event")
    review.add_argument("url")
    review.add_argument("event_id",help="Stable provider delivery/event ID")
    review.add_argument("kind",choices=["review","ci","closed","merged","reopened"])
    return p


def doctor(root, profiles, line):
    probe_id = str(uuid.uuid4())
    location = root / "probes" / probe_id
    location.mkdir(parents=True)
    revision, home, plugin = runner.profile(line,profiles)
    trace_file = location / "otlp.ndjson"
    with LocalTraceReceiver(trace_file) as receiver:
        trace = Trace(receiver.endpoint,task_id="capability-audit",run_id=probe_id,line=line,
                      attributes={"skill.revision":revision})
        result = {"line":line,"revision":revision,"native_skill_invocations":None,
                  "model_usage":None,"cost":None,"run_id":probe_id,"trace_id":trace.trace_id}
        try:
            with trace.span("factory.capability_probe") as span:
                result["profiles"] = str(profiles)
                if line == "lauren":
                    result.update(host="cursor", authenticated=None, skills=[],
                                  installed_skills=len(plugin.skills), installed_agents=len(plugin.agents),
                                  execution_gate="cursor_delegated_model_policy_unverified")
                    span.event("profile.verified", attributes={"host.name":"cursor",
                               "skill.installed":len(plugin.skills), "model.policy_verified":False})
                else:
                    with runner.host(line,home,location,plugin,read_only=True) as rt:
                        caps=rt.capabilities()
                        result["authenticated"]=caps["authenticated"]
                        enabled=rt.isolate_skills()
                        result["host"]="codex"
                        result["skills"]=sorted(s["name"] for s in enabled)
                        result["astra_ultra_advertised"]="ultra" in caps["models"].get("gpt-6.2-astra", [])
                        if plugin is not None:
                            result["plugin_id"]=plugin["plugin_id"]
                    span.event("host.discovery",attributes={"host.name":result["host"],
                               "skill.count":len(result["skills"]),"auth.available":result["authenticated"]})
                data=runner.json_bytes(result)
                runner.write_once(location/"discovery.json",data)
                span.add_artifact(f"probes/{probe_id}/discovery.json",kind="capability",
                                  sha256=hashlib.sha256(data).hexdigest())
                span.set_status("ok")
        finally:
            trace.export()
    if not all(f(path,trace.trace_id) for f,path in (
            (read_trace,trace_file),(read_metrics,receiver.metrics_path),(read_logs,receiver.logs_path))):
        raise RuntimeError("Missing trace, metric or log receipt")
    summary = {"checked_at": datetime.now(timezone.utc).isoformat(), "profiles": str(profiles),
               "authenticated": result["authenticated"], "skills_discovered": len(result["skills"]),
               "revision": revision, "run_id": probe_id, "host": result["host"]}
    if "execution_gate" in result:
        summary.update(execution_gate=result["execution_gate"], installed_skills=result["installed_skills"])
    latest = root / "probes" / "latest"
    latest.mkdir(exist_ok=True)
    staging = latest / (probe_id + ".tmp")
    try:
        staging.write_bytes(runner.json_bytes(summary))
        staging.replace(latest / (line + ".json"))
    finally:
        staging.unlink(missing_ok=True)
    return {**result,"artifact":str(location/"discovery.json")}


def main(argv=None):
    args=parser().parse_args(argv)
    root=args.root.resolve()
    profiles=(args.profiles or root/"profiles").resolve()
    store=Store(args.state or root/"factory.db")
    action=args.command
    if action == "import-pool":
        data=json.loads(args.file.read_text(encoding="utf-8-sig"))
        result=[store.discover(row["id"],row["identifier"],row["upstream"],row["title"]) for row in data]
    elif action == "pool":
        result=store.pool()
    elif action == "inbox":
        result=store.inbox()
    elif action == "status":
        gates = store.inbox()
        tasks = store.pool()
        result = {"unclaimed_in_pool": [t["identifier"] for t in tasks if t["state"] == "ready"],
                  "lines": {line: [{"task": t["identifier"], "state": t["state"],
                                    "session": t["native_session"], "run": t["active_run"], "pr": t["pr"]}
                                   for t in tasks if t["owner"] == line] for line in LINES},
                  "decisions": gates}
        checks = {}
        for line in LINES:
            path = root / "probes" / "latest" / (line + ".json")
            if path.is_file():
                check = json.loads(path.read_bytes())
                if Path(check.pop("profiles")) == profiles and check.get("host") in {"codex","cursor"}:
                    checks[line] = check
        result["last_host_checks"] = checks
        heartbeat = root / "dispatcher-status.json"
        if heartbeat.is_file():
            result["last_dispatcher_heartbeat"] = json.loads(heartbeat.read_bytes())
        native = root / "symphony-admission.json"
        result["last_symphony_admission"] = json.loads(native.read_bytes()) if native.is_file() else None
    elif action == "request-turn":
        store.request_turn(args.task, args.reason)
        result = store.get(args.task)
    elif action == "recover":
        store.recover(args.task, args.stopped_run)
        result = store.get(args.task)
    elif action == "doctor":
        result=doctor(root,profiles,args.line)
    elif action == "login":
        _, home, plugin = runner.profile(args.line, profiles)
        rt = runner.host(args.line, home, root, plugin, read_only=True)
        return subprocess.run(rt.command + ["login", "--device-auth"], cwd=root, env=rt.env).returncode
    elif action == "sync-skills":
        if args.line == "lauren":
            from .lauren_native import sync
            result = sync(profiles / args.line)
        else:
            from .native_skills import sync
            current=sync(args.line,profiles/args.line)
            result={"line":args.line,"revision":current.revision,"skills":len(current.skills)}
    elif action == "claim":
        result=runner.claim(store,args.task,args.line,args.repo,args.base,root,profiles)
    elif action == "choose":
        result={"task":runner.choose(store,args.line,profiles,root,args.model,args.effort)}
    elif action == "run":
        result=runner.run(store,args.task,root,profiles,args.model,args.effort,
                          instructions=args.instructions,read_only=not args.local_write)
    elif action == "ask":
        result={"decision":store.park(args.task,args.line,question=args.question)}
    elif action == "answer":
        result={"task":store.decide(args.decision,args.text)}
    elif action == "track-pr":
        store.park(args.task,args.line,pr=args.url)
        result=store.get(args.task)
    else:
        result=store.feedback(args.url,args.event_id,args.kind)
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError,ValueError,RuntimeError) as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
