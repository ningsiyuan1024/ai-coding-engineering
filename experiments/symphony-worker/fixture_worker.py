"""In-memory-tracker smoke worker backed by a disposable factory Store."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factory.state import Store


FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"


def main(path, operation, *args):
    store = Store(path)
    if operation == "setup":
        for number, (task, owner) in enumerate(((FIRST, "sylan"), (SECOND, "matt")), 1):
            store.discover(
                task, f"SMOKE-{number}", f"https://github.com/owner/repository/issues/{number}",
                f"Disposable worker {number}",
            )
            store.claim(task, owner, "fixture-revision", path.parent / "worktrees" / task, "fixture-base")
        return {"tasks": [FIRST, SECOND]}
    if operation == "decide":
        task = store.decide(args[0], "Resume the disposable operation")
        return {"task": task, "owner": store.get(task)["owner"], "runnable": store.needs_turn(task)}
    task, outcome = args
    current = store.get(task)
    if operation != "run" or not store.needs_turn(task):
        raise ValueError("Fixture run was not admitted by the factory Store")
    run_id = store.begin(task, current["owner"], "none", "none")
    gate = store.finish(
        task, run_id, outcome,
        question="Resume this disposable task?" if outcome == "waiting_human" else None,
        pr="https://github.com/owner/repository/pull/3" if outcome == "waiting_upstream" else None,
    )
    return {
        "task": task, "run": run_id, "owner": current["owner"], "state": store.get(task)["state"],
        "decision": gate, "runnable": store.needs_turn(task),
    }


if __name__ == "__main__":
    database, operation, *arguments = sys.argv[1:]
    print(json.dumps(main(Path(database), operation, *arguments)))
