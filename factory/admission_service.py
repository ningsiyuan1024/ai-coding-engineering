"""One admission and maintenance pass for the supervising Symphony instance."""

import os
from pathlib import Path
import sys

from . import dispatch


def main():
    root = Path(os.environ["FACTORY_ROOT"]).resolve()
    mode = os.environ.get("FACTORY_MODE", "investigate")
    if mode not in ("investigate", "implement"):
        raise ValueError("FACTORY_MODE must be investigate or implement")
    args = ["--maintain", "--root", str(root), "--receipt", str(root / "symphony-admission.json"),
            "--state", os.environ.get("FACTORY_STATE", str(root / "factory.db")),
            "--profiles", os.environ.get("FACTORY_PROFILES", str(root / "profiles")),
            "--plan", os.environ.get("FACTORY_PLAN", str(root / "model-plan.json")),
            "--sources", os.environ.get("FACTORY_SOURCES", str(root / "sources.json")),
            "--symphony-root", os.environ["FACTORY_SYMPHONY_ROOT"]]
    if mode == "implement":
        args.append("--local-write")
    return dispatch.main(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        print(f"Factory admission failed ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
