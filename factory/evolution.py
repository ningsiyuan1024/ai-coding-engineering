"""Offline replay of a Sylan pull-request triage policy."""

import argparse
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory


BASELINE = "sylan-pr-triage-v1"
CANDIDATE = "sylan-pr-triage-v2"
DEFAULT_VERSION_FILE = (
    Path(__file__).resolve().parents[1] / "workflows/sylan-versions/pr-triage.json"
)
ACTION = "owner_action"
WAIT = "no_owner_action"
UNKNOWN = "unknown"
LABELS = {"待我处理": ACTION, "待上游评审": WAIT}


@dataclass(frozen=True)
class TriageInput:
    mergeable: str | None
    review_decision: str | None
    current_threads: bool | None


def classify(revision: str, observation: TriageInput) -> str:
    if revision not in (BASELINE, CANDIDATE):
        raise ValueError(f"Unknown Sylan revision: {revision}")
    if observation.mergeable == "CONFLICTING" or observation.review_decision == "CHANGES_REQUESTED":
        return ACTION
    if revision == CANDIDATE:
        if observation.current_threads is True:
            return ACTION
        if observation.current_threads is None:
            return UNKNOWN
    # The endpoint uses null for the absence of a formal review decision.
    if observation.mergeable != "MERGEABLE" or observation.review_decision not in (
        None, "APPROVED", "REVIEW_REQUIRED"
    ):
        return UNKNOWN
    return WAIT


def _instant(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("Snapshot timestamps must include a timezone")
    return timestamp


def _observations(snapshot, cutoff):
    observed_at = _instant(snapshot["checked_at"])
    if observed_at != _instant(cutoff) or snapshot["open_search_complete"] is not True:
        raise ValueError("Expected the complete, pinned open-PR snapshot")
    seen = set()
    excluded = []
    cohort = []
    for row in snapshot["prs"]:
        identity = f'{row["repo"]}#{row["number"]}'
        if identity in seen:
            raise ValueError(f"Duplicate PR: {identity}")
        seen.add(identity)
        if row["state"] != "OPEN" or row["is_draft"]:
            excluded.append(identity)
            continue
        if not (_instant(row["created_at"]) <= _instant(row["updated_at"]) <= observed_at):
            raise ValueError(f"Invalid observation time for {identity}")
        threads = row.get("unresolved_current_threads")
        complete = row.get("review_threads_complete") is True and isinstance(threads, list)
        observation = TriageInput(
            row.get("mergeable"),
            row["review_decision"],
            bool(threads) if complete else None,
        )
        cohort.append((identity, row["head"], observation, row.get("stage")))
    return cohort, excluded


def _metrics(rows, revision):
    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    counts = dict(tp=0, fp=0, tn=0, fn=0, abstained=0, unlabeled=0)
    for row in rows:
        label, guess = row["label"], row[revision]
        if label is None:
            counts["unlabeled"] += 1
        elif guess == UNKNOWN:
            counts["abstained"] += 1
        elif guess == ACTION:
            counts["tp" if label == ACTION else "fp"] += 1
        else:
            counts["fn" if label == ACTION else "tn"] += 1
    tp, fp, tn, fn = (counts[key] for key in ("tp", "fp", "tn", "fn"))
    labeled = len(rows) - counts["unlabeled"]
    return counts | {
        "precision": ratio(tp, tp + fp),
        "recall_on_classified": ratio(tp, tp + fn),
        "coverage_on_labeled": ratio(tp + fp + tn + fn, labeled),
    }


def _load_active(version_file):
    revision = json.loads(version_file.read_bytes())["active_revision"]
    if revision not in (BASELINE, CANDIDATE):
        raise ValueError(f"Unknown active revision: {revision}")
    return revision


def verify_rollback(version_file, observations):
    """Use an isolated version pointer and replay the same frozen inputs."""
    def replay_active(path):
        revision = _load_active(path)
        return [classify(revision, observation) for _, _, observation, _ in observations]

    original = version_file.read_bytes()
    if _load_active(version_file) != BASELINE:
        raise ValueError("This experiment expects the baseline to remain active")
    with TemporaryDirectory() as scratch:
        pointer = Path(scratch) / version_file.name
        pointer.write_bytes(original)
        before = replay_active(pointer)
        trial = json.loads(original)
        trial["active_revision"] = CANDIDATE
        pointer.write_text(json.dumps(trial), encoding="utf-8")
        during = replay_active(pointer)
        pointer.write_bytes(original)
        after = replay_active(pointer)
        active_after = _load_active(pointer)
    return {
        "trial": CANDIDATE,
        "restored": active_after,
        "changed_decisions_during_trial": sum(a != b for a, b in zip(before, during, strict=True)),
        "exact_decisions_restored": before == after,
        "project_version_file_unchanged": version_file.read_bytes() == original,
    }


def run_experiment(snapshot_path: Path, version_file: Path = DEFAULT_VERSION_FILE) -> dict:
    manifest = json.loads(version_file.read_bytes())
    raw = snapshot_path.read_bytes()
    fingerprint = sha256(raw).hexdigest()
    if fingerprint != manifest["snapshot"]["sha256"]:
        raise ValueError("Snapshot SHA-256 does not match the pinned evidence")
    cohort, excluded = _observations(json.loads(raw), manifest["snapshot"]["checked_at"])
    rows = []
    for identity, head, observation, stage in cohort:
        # Policies receive only the fields available at the observation time.
        baseline = classify(BASELINE, observation)
        candidate = classify(CANDIDATE, observation)
        rows.append({
            "pr": identity,
            "head": head,
            "observed": {
                "mergeable": observation.mergeable,
                "review_decision": observation.review_decision,
                "current_threads": observation.current_threads,
            },
            BASELINE: baseline,
            CANDIDATE: candidate,
            "historical_stage": stage,
            "label": LABELS.get(stage),
        })
    baseline = _metrics(rows, BASELINE)
    candidate = _metrics(rows, CANDIDATE)
    passes = (
        candidate["tp"] > baseline["tp"]
        and candidate["fp"] <= baseline["fp"]
        and candidate["abstained"] <= baseline["abstained"]
    )
    return {
        "experiment": manifest["experiment"],
        "source_kind": "retrospective_external_contribution_records",
        "snapshot": manifest["snapshot"],
        "excluded": excluded,
        "rows": rows,
        "metrics": {BASELINE: baseline, CANDIDATE: candidate},
        "decision": "eligible_for_review" if passes else "reject",
        "active_revision": _load_active(version_file),
        "rollback": verify_rollback(version_file, cohort),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="Pinned historical PR snapshot")
    parser.add_argument("--version-file", type=Path, default=DEFAULT_VERSION_FILE)
    args = parser.parse_args()
    try:
        report = run_experiment(args.snapshot, args.version_file)
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, f"evolution: {error}\n")
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
