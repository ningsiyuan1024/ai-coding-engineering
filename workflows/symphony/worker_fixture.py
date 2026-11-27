"""Disposable CLI fixture for the native worker port and scheduler smoke."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main(operation, identifier):
    root = Path.cwd()
    if operation == "eligible":
        time.sleep(0.5)
        return {"eligible": (root / ("allow-" + identifier)).is_file()}
    if operation == "execute":
        (root / ("allow-" + identifier)).unlink()
        (root / ("started-" + identifier)).write_text("running", encoding="utf-8")
        deadline = time.monotonic() + 20
        while not (root / ("release-" + identifier)).is_file():
            if time.monotonic() > deadline:
                raise TimeoutError("Fixture orchestration did not release the worker")
            time.sleep(0.03)
        return {"disposition": "waiting_human"}
    if operation == "cooperative":
        (root / "cooperative-started").write_text("started", encoding="utf-8")
        message = sys.stdin.readline().strip()
        (root / "cooperative-cancelled").write_text(message, encoding="utf-8")
        return {"disposition": "idle"}
    if operation == "heartbeat":
        deadline = time.monotonic() + 18
        with (root / "heartbeat").open("wb", buffering=0) as stream:
            while time.monotonic() < deadline:
                stream.write(b".")
                time.sleep(0.1)
        return {"disposition": "idle"}
    if operation == "stubborn":
        child = subprocess.Popen(
            [sys.executable, "-B", __file__, "heartbeat", identifier],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        (root / "stubborn-started").write_text(
            json.dumps({"worker": os.getpid(), "child": child.pid}), encoding="utf-8"
        )
        child.wait()
        return {"disposition": "idle"}
    raise ValueError("Unexpected disposable fixture command")


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:])), flush=True)
