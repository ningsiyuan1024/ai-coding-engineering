import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from factory import evolution


class TriageTests(unittest.TestCase):
    def test_candidate_adds_current_thread_signal(self):
        observed = evolution.TriageInput("MERGEABLE", None, True)
        self.assertEqual(evolution.classify(evolution.BASELINE, observed), evolution.WAIT)
        self.assertEqual(evolution.classify(evolution.CANDIDATE, observed), evolution.ACTION)

    def test_hard_signals_win_over_unknown_thread_coverage(self):
        for observed in (
            evolution.TriageInput("CONFLICTING", None, None),
            evolution.TriageInput("UNKNOWN", "CHANGES_REQUESTED", None),
        ):
            for revision in (evolution.BASELINE, evolution.CANDIDATE):
                self.assertEqual(evolution.classify(revision, observed), evolution.ACTION)

    def test_candidate_abstains_on_incomplete_threads(self):
        observed = evolution.TriageInput("MERGEABLE", "APPROVED", None)
        self.assertEqual(evolution.classify(evolution.CANDIDATE, observed), evolution.UNKNOWN)

    def test_projection_keeps_unknowns_and_enforces_cutoff(self):
        observed_at = "2026-11-27T01:04:17Z"
        row = {
            "repo": "example/repo", "number": 1, "state": "OPEN", "is_draft": False,
            "created_at": observed_at, "updated_at": observed_at, "head": "example",
            "mergeable": "MERGEABLE", "review_decision": None, "stage": "待我处理",
            "unresolved_current_threads": [{"path": "example"}],
            "review_threads_complete": False,
        }
        snapshot = {"checked_at": observed_at, "open_search_complete": True, "prs": [row]}
        cohort, excluded = evolution._observations(snapshot, observed_at)
        self.assertEqual(excluded, [])
        self.assertIsNone(cohort[0][2].current_threads)
        row["stage"] = "待上游评审"
        changed_labels, _ = evolution._observations(snapshot, observed_at)
        self.assertEqual(cohort[0][2], changed_labels[0][2])
        row["updated_at"] = "2026-11-27T01:04:18Z"
        with self.assertRaisesRegex(ValueError, "Invalid observation time"):
            evolution._observations(snapshot, observed_at)

    def test_metrics_preserve_abstentions_and_unlabeled_rows(self):
        rule = evolution.CANDIDATE
        rows = [
            {"label": evolution.ACTION, rule: evolution.UNKNOWN},
            {"label": evolution.WAIT, rule: evolution.ACTION},
            {"label": None, rule: evolution.WAIT},
        ]
        metrics = evolution._metrics(rows, rule)
        self.assertEqual(metrics["abstained"], 1)
        self.assertEqual(metrics["unlabeled"], 1)
        self.assertEqual(metrics["fp"], 1)
        self.assertEqual(metrics["coverage_on_labeled"], 0.5)
        self.assertIsNone(metrics["recall_on_classified"])

    def test_digest_mismatch_stops_replay(self):
        with TemporaryDirectory() as scratch:
            source = Path(scratch) / "different.json"
            source.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                evolution.run_experiment(source)

    def test_rollback_restores_baseline_predictions(self):
        version_file = evolution.DEFAULT_VERSION_FILE
        observed = evolution.TriageInput("MERGEABLE", None, True)
        report = evolution.verify_rollback(version_file, [("example/repo#1", "head", observed, None)])
        self.assertEqual(report["changed_decisions_during_trial"], 1)
        self.assertEqual(report["restored"], evolution.BASELINE)
        self.assertTrue(report["exact_decisions_restored"])
        self.assertTrue(report["project_version_file_unchanged"])
        self.assertEqual(json.loads(version_file.read_bytes())["active_revision"], evolution.BASELINE)
