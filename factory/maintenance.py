"""Poll PR feedback and refresh already-installed native profiles from the watch loop."""

from contextlib import closing
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from . import github_events, native_skills


PR_INTERVAL = 5 * 60
SYNC_INTERVAL = 7 * 24 * 60 * 60
RETRY_INTERVAL = 60
MAX_RETRY_INTERVAL = 60 * 60


def _poll(store):
    report = github_events.poll_once(store)
    return {
        "state": "partial" if report["errors"] else "ok",
        "checked": len(report["pull_requests"]),
        "errors": len(report["errors"]),
        # poll_once delegates deduplication to Store.feedback's durable event key.
        "events_observed": sum(len(pr["events"]) for pr in report["pull_requests"]),
    }


def _refresh(line, profile):
    registry = profile / "native" / "versions.sqlite3"
    with closing(sqlite3.connect(registry.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT 1 FROM versions LIMIT 1").fetchone() is None:
            return {"state": "unprovisioned"}
    selected = native_skills.sync(line, profile)
    return {"state": "ok", "revision": selected.revision}


def _scheduled(directory, job, target, interval, action):
    # Each job locks only its own scheduling database. Network reads never hold
    # a coordinator transaction or block maintenance for another line. SQLite
    # releases the lock on process death; poll and sync are safe to replay.
    identity = os.path.normcase(str(Path(target).resolve())).encode()
    key = hashlib.sha256(identity).hexdigest()
    path = directory / f"{job}-{key}.sqlite3"
    with closing(sqlite3.connect(path, timeout=0)) as db, db:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return {"state": "busy"}
            raise
        db.execute("""CREATE TABLE IF NOT EXISTS schedule
                   (id INTEGER PRIMARY KEY CHECK(id=1), next_due INTEGER NOT NULL,
                    failures INTEGER NOT NULL, result TEXT NOT NULL)""")
        previous = db.execute("SELECT next_due, failures, result FROM schedule WHERE id=1").fetchone()
        if previous and time.time() < previous[0]:
            return {"state": "deferred", "last": json.loads(previous[2]), "next_due": previous[0]}
        try:
            result = action()
        except Exception as exc:
            # Preserve progress in independent jobs. Do not log subprocess
            # arguments, raw upstream error bodies, or private profile paths.
            result = {"state": "error", "error_type": type(exc).__name__}
        if result["state"] == "unprovisioned":
            return result
        failures = 0
        delay = interval
        if result["state"] != "ok":
            failures = (previous[1] if previous else 0) + 1
            delay = min(MAX_RETRY_INTERVAL, RETRY_INTERVAL * 2 ** min(failures - 1, 6))
        next_due = int(time.time()) + delay
        db.execute("INSERT OR REPLACE INTO schedule VALUES(1, ?, ?, ?)",
                   (next_due, failures, json.dumps(result)))
        return {**result, "next_due": next_due}


def maintain(store, root, profiles):
    """Perform due reads and return once. All timings survive watch restarts."""
    directory = Path(root).resolve() / "maintenance"
    directory.mkdir(parents=True, exist_ok=True)
    profiles = Path(profiles).resolve()
    github = _scheduled(directory, "github", store.path, PR_INTERVAL, partial(_poll, store))
    lines = {}
    for line in native_skills.SOURCES:
        profile = profiles / line
        native = profile / "native"
        if not (native / "owner" / line).is_file() or not (native / "versions.sqlite3").is_file():
            lines[line] = {"state": "unprovisioned"}
            continue
        lines[line] = _scheduled(directory, line, profile, SYNC_INTERVAL, partial(_refresh, line, profile))
    return {"github": github, "profiles": lines}
