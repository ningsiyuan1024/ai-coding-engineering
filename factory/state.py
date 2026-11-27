"""Single-host claim coordinator and durable task/session handoff."""

import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


LINES = ("sylan", "matt", "lauren", "addy")


class Conflict(ValueError):
    """The requested transition no longer matches the live task."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def upstream_key(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or len(parts) != 4 or parts[2] not in ("issues", "pull")
            or not parts[3].isdecimal() or int(parts[3]) < 1
            or not all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) for part in parts[:2])
            or parsed.query or parsed.fragment):
        raise ValueError("Expected a canonical GitHub issue or PR URL")
    return "/".join([parts[0].lower(), parts[1].lower(), str(int(parts[3]))])


def _pr_key(url):
    key = upstream_key(url)
    if "/pull/" not in urlsplit(url).path:
        raise ValueError("Expected a canonical GitHub PR URL")
    return key


def _trace_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value) or int(value, 16) == 0:
        raise ValueError("Expected a nonzero 32-digit hexadecimal trace ID")
    return value.lower()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, identifier TEXT NOT NULL, upstream TEXT NOT NULL,
                upstream_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
                owner TEXT, revision TEXT, worktree TEXT UNIQUE, base TEXT,
                native_session TEXT, pr TEXT UNIQUE, active_run TEXT,
                updated TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                line TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                state TEXT NOT NULL, started TEXT NOT NULL, ended TEXT, trace_id TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                task TEXT NOT NULL REFERENCES tasks(id), key TEXT NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(task, key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id),
                prompt TEXT NOT NULL, answer TEXT, created TEXT NOT NULL, answered TEXT)""")
            self._migrate(db)

    @staticmethod
    def _migrate(db):
        tasks = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "active_decision" not in tasks:
            db.execute("ALTER TABLE tasks ADD COLUMN active_decision TEXT")
            db.execute("""UPDATE tasks SET active_decision=(
                SELECT id FROM decisions WHERE task=tasks.id AND answered IS NULL
                ORDER BY created DESC, rowid DESC LIMIT 1)
                WHERE state='waiting_human'""")
        if "pr_key" not in tasks:
            db.execute("ALTER TABLE tasks ADD COLUMN pr_key TEXT")
            owners = {}
            for row in db.execute("SELECT id, pr FROM tasks WHERE pr IS NOT NULL").fetchall():
                key = _pr_key(row["pr"])
                if key in owners:
                    raise Conflict("Existing tasks claim the same canonical PR")
                owners[key] = row["id"]
            db.executemany("UPDATE tasks SET pr_key=? WHERE id=?", owners.items())
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_pr_key_unique ON tasks(pr_key)")
        runs = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
        if "feedback_pending" not in runs:
            db.execute("ALTER TABLE runs ADD COLUMN feedback_pending INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, task, kind, data, key=None):
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                   (task, key or str(uuid.uuid4()), kind, json.dumps(data), now()))

    @staticmethod
    def _task(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown Linear issue UUID")
        return dict(row)

    def get(self, task_id):
        with self.transaction() as db:
            return self._task(db, task_id)

    def discover(self, task_id, identifier, upstream, title):
        task_id = str(uuid.UUID(task_id))
        key = upstream_key(upstream)
        with self.transaction() as db:
            same = db.execute("SELECT id, upstream_key FROM tasks WHERE id=? OR upstream_key=?",
                              (task_id, key)).fetchall()
            if same and (len(same) != 1 or same[0]["id"] != task_id or same[0]["upstream_key"] != key):
                raise Conflict("Linear identity and upstream identity disagree")
            if same:
                db.execute("UPDATE tasks SET identifier=?, title=? WHERE id=?",
                           (identifier, title, task_id))
            else:
                db.execute("""INSERT INTO tasks
                    (id,identifier,upstream,upstream_key,title,state,updated)
                    VALUES (?,?,?,?,?,'ready',?)""", (task_id,identifier,upstream,key,title,now()))
                self._event(db, task_id, "discovered", {"upstream": upstream})
            return self._task(db, task_id)

    def claim(self, task_id, line, revision, worktree, base):
        if line not in LINES or not revision or not base:
            raise ValueError("Claim requires a known line and pinned skill and git revisions")
        worktree = str(Path(worktree).resolve())
        with self.transaction() as db:
            row = self._task(db, task_id)
            if row["owner"]:
                if (line,revision,worktree,base) == tuple(row[k] for k in ("owner","revision","worktree","base")):
                    return row
                raise Conflict("Already claimed; original line and revisions are pinned")
            db.execute("""UPDATE tasks SET owner=?,revision=?,worktree=?,base=?,state='claimed',
                          updated=? WHERE id=?""", (line,revision,worktree,base,now(),task_id))
            self._event(db, task_id, "claimed", {"line": line, "revision": revision, "base": base})
            return self._task(db, task_id)

    def begin(self, task_id, line, model, effort, trace_id=None):
        if trace_id is not None:
            trace_id = _trace_id(trace_id)
        with self.transaction() as db:
            row = self._task(db,task_id)
            if row["owner"] != line or row["state"] not in ("claimed","ready_to_resume","failed"):
                raise Conflict("Task is not runnable by this line")
            run = str(uuid.uuid4())
            db.execute("""INSERT INTO runs (id,task,line,model,effort,state,started,trace_id)
                VALUES(?,?,?,?,?,'running',?,?)""",
                       (run,task_id,line,model,effort,now(),trace_id))
            db.execute("UPDATE tasks SET state='running',active_run=?,updated=? WHERE id=?",
                       (run,now(),task_id))
            self._event(db,task_id,"run_started",{"run":run,"model":model,"effort":effort})
            return run

    @staticmethod
    def _attach_trace(db, run, trace_id):
        trace_id = _trace_id(trace_id)
        row = db.execute("SELECT trace_id FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise ValueError("Unknown factory run")
        current = row["trace_id"]
        if current is None:
            db.execute("UPDATE runs SET trace_id=? WHERE id=?", (trace_id, run))
        elif current.lower() != trace_id:
            raise Conflict("Run is already linked to a different trace")

    def attach_trace(self, run, trace_id):
        """Pin a run's trace, including evidence arriving after it was superseded."""
        with self.transaction() as db:
            self._attach_trace(db, run, trace_id)

    def bind(self, task_id, run, session):
        if not session:
            raise ValueError("Empty native session ID")
        with self.transaction() as db:
            row = self._task(db,task_id)
            if row["active_run"] != run or row["native_session"] not in (None,session):
                raise Conflict("Native session or run differs from the current task")
            db.execute("UPDATE tasks SET native_session=?,updated=? WHERE id=?", (session,now(),task_id))

    @staticmethod
    def _check_pr_owner(db, task, key):
        if key is None:
            return
        if key.rsplit("/", 1)[0] != task["upstream_key"].rsplit("/", 1)[0]:
            raise ValueError("Expected a PR in this task's upstream repository")
        if task["pr_key"] and task["pr_key"] != key:
            raise Conflict("A different PR already belongs to this task")
        owner = db.execute("SELECT id FROM tasks WHERE pr_key=?", (key,)).fetchone()
        if owner is not None and owner["id"] != task["id"]:
            raise Conflict("PR already belongs to another task")

    @staticmethod
    def _new_decision(db, task_id, question):
        gate = str(uuid.uuid4())
        db.execute("INSERT INTO decisions VALUES(?,?,?,NULL,?,NULL)", (gate, task_id, question, now()))
        return gate

    def finish(self, task_id, run, outcome, *, question=None, pr=None, trace_id=None):
        if outcome not in ("waiting_human","waiting_upstream","ready_to_resume","failed","closed"):
            raise ValueError("Unsupported task outcome")
        if outcome == "waiting_human" and not (question or "").strip():
            raise ValueError("A human decision needs a concrete question")
        pr_key = _pr_key(pr) if pr else None
        with self.transaction() as db:
            row = self._task(db,task_id)
            if row["active_run"] != run or row["state"] != "running":
                raise Conflict("Run has been superseded or finished")
            self._check_pr_owner(db, row, pr_key)
            if trace_id is not None:
                self._attach_trace(db, run, trace_id)
            pending = db.execute("SELECT feedback_pending FROM runs WHERE id=?", (run,)).fetchone()[0]
            task_state = "ready_to_resume" if outcome == "waiting_upstream" and pending else outcome
            gate = self._new_decision(db, task_id, question) if outcome == "waiting_human" else None
            db.execute("UPDATE runs SET state=?,ended=? WHERE id=?", (outcome,now(),run))
            db.execute("""UPDATE tasks SET state=?,active_run=NULL,active_decision=?,
                pr=COALESCE(pr,?),pr_key=COALESCE(pr_key,?),updated=? WHERE id=?""",
                       (task_state,gate,pr,pr_key,now(),task_id))
            self._event(db, task_id, "run_finished",
                        {"run": run, "outcome": outcome, "task_state": task_state, "decision": gate})
            return gate

    def park(self, task_id, line, *, question=None, pr=None):
        """Record a decision request or PR link while the owning line is idle."""
        if question is not None and (not isinstance(question, str) or not question.strip()):
            raise ValueError("A human decision needs a concrete question")
        key = _pr_key(pr) if pr is not None else None
        if question is None and key is None:
            raise ValueError("Provide a decision question or a PR")
        with self.transaction() as db:
            task = self._task(db, task_id)
            if (
                line not in LINES or task["owner"] != line or task["active_run"] is not None
                or task["state"] not in ("claimed", "ready_to_resume", "failed", "waiting_upstream", "waiting_human")
            ):
                raise Conflict("Task is not idle under this line's ownership")
            self._check_pr_owner(db, task, key)
            gate = task["active_decision"]
            state = task["state"]
            if question is not None:
                if gate is None:
                    gate = self._new_decision(db, task_id, question)
                else:
                    pending = db.execute("SELECT prompt FROM decisions WHERE id=?", (gate,)).fetchone()
                    if pending["prompt"] != question:
                        raise Conflict("A different decision is already pending")
                state = "waiting_human"
            elif gate is None:
                state = "waiting_upstream"
            linked_pr = key is not None and task["pr_key"] is None
            if linked_pr or gate != task["active_decision"] or state != task["state"]:
                db.execute("""UPDATE tasks SET state=?,active_decision=?,pr=COALESCE(pr,?),
                    pr_key=COALESCE(pr_key,?),updated=? WHERE id=?""",
                           (state, gate, pr, key, now(), task_id))
                self._event(db, task_id, "parked", {"state": state, "decision": gate, "linked_pr": linked_pr})
            return gate if question is not None else None

    def decide(self, decision_id, answer):
        if not answer.strip():
            raise ValueError("Empty decision")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if not row:
                raise ValueError("Unknown decision")
            if row["answered"]:
                if row["answer"] == answer:
                    return row["task"]
                raise Conflict("Decision already recorded")
            task = self._task(db,row["task"])
            if task["state"] != "waiting_human" or task["active_decision"] != decision_id:
                raise Conflict("Task is no longer waiting on this decision")
            db.execute("UPDATE decisions SET answer=?,answered=? WHERE id=?",(answer,now(),decision_id))
            db.execute("""UPDATE tasks SET state='ready_to_resume',active_decision=NULL,updated=?
                WHERE id=?""", (now(), row["task"]))
            self._event(db,row["task"],"decision_received",{"decision":decision_id})
            return row["task"]

    def latest_answer(self, task_id):
        """Return this task's most recently recorded human answer."""
        with self.transaction() as db:
            self._task(db, task_id)
            row = db.execute("""SELECT answer FROM decisions WHERE task=? AND answered IS NOT NULL
                ORDER BY answered DESC, rowid DESC LIMIT 1""", (task_id,)).fetchone()
            return row["answer"] if row is not None else None

    def inbox(self):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("""SELECT d.id,d.task,t.identifier,t.owner,
                t.native_session,d.prompt FROM decisions d JOIN tasks t ON t.id=d.task
                WHERE d.answered IS NULL AND t.state='waiting_human' AND t.active_decision=d.id
                ORDER BY d.created""")]

    def pool(self):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tasks ORDER BY identifier")]

    def request_turn(self, task_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reason is required to request another turn")
        with self.transaction() as db:
            task = self._task(db, task_id)
            if not task["owner"] or task["state"] not in ("claimed", "ready_to_resume", "failed"):
                raise Conflict("Task is not available for a new turn")
            self._event(db, task_id, "turn_requested", {"reason": reason})

    def needs_turn(self, task_id):
        """Only an initial claim or a subsequent wake-up warrants paid execution."""
        with self.transaction() as db:
            task = self._task(db, task_id)
            if task["state"] not in ("claimed", "ready_to_resume", "failed"):
                return False
            last = db.execute("""SELECT type FROM events WHERE task=? AND type IN
                ('run_started','claimed','decision_received','turn_requested','pr_review','pr_ci','pr_reopened')
                ORDER BY rowid DESC LIMIT 1""", (task_id,)).fetchone()
            return last is not None and last["type"] != "run_started"

    def recover(self, task_id, run_id):
        """Release a terminated host's reservation by exact run ID; preserve its native session."""
        with self.transaction() as db:
            task = self._task(db, task_id)
            if task["active_run"] != run_id or task["state"] != "running":
                raise Conflict("Recovery does not identify the current run")
            db.execute("UPDATE runs SET state='interrupted',ended=? WHERE id=?", (now(), run_id))
            db.execute("UPDATE tasks SET state='failed',active_run=NULL,updated=? WHERE id=?",
                       (now(), task_id))
            self._event(db, task_id, "turn_requested", {"interrupted_run": run_id})

    def feedback(self, pr, event_key, kind):
        if kind not in ("review","ci","closed","merged","reopened") or not event_key:
            raise ValueError("Unsupported upstream event")
        pr_key = _pr_key(pr)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE pr_key=?", (pr_key,)).fetchone()
            if not row:
                raise ValueError("Untracked PR")
            existing = db.execute("SELECT type FROM events WHERE task=? AND key=?",(row["id"],event_key)).fetchone()
            if existing:
                if existing["type"] != "pr_"+kind:
                    raise Conflict("Event identity already used")
                return self._task(db,row["id"])
            self._event(db,row["id"],"pr_"+kind,{"pr":pr},event_key)
            if kind in ("closed","merged"):
                if row["active_run"]:
                    db.execute("UPDATE runs SET state='superseded',ended=? WHERE id=?",(now(),row["active_run"]))
                db.execute("""UPDATE tasks SET state='closed',active_run=NULL,active_decision=NULL,
                    updated=? WHERE id=?""", (now(), row["id"]))
            elif row["active_run"] and kind in ("review", "ci"):
                db.execute("UPDATE runs SET feedback_pending=1 WHERE id=?", (row["active_run"],))
            elif row["state"] == "waiting_upstream" or (kind == "reopened" and row["state"] == "closed"):
                db.execute("UPDATE tasks SET state='ready_to_resume',updated=? WHERE id=?",(now(),row["id"]))
            return self._task(db,row["id"])
