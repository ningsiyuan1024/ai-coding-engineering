import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import lauren_native
from factory.native_skills import Source, _git_hash, _tree_hash


class PstackProfileTests(unittest.TestCase):
    def setUp(self):
        workspace = self.enterContext(tempfile.TemporaryDirectory(dir=lauren_native._io_path(Path.cwd())))
        self.root = Path(workspace) / "lauren"
        self.archive = Path(workspace) / "cursor.tar.gz"
        self.files = {
            ".cursor-plugin/plugin.json": json.dumps({
                "name": "pstack", "version": "0.15.48", "skills": "./skills/", "agents": "./agents/",
            }).encode(),
            "plugin.json": b'{"name":"pstack"}\n',
            "skills/poteto-mode/SKILL.md": b"---\nname: poteto-mode\n---\nOriginal router.\n",
            "skills/setup-pstack/SKILL.md": b"---\nname: setup-pstack\n---\nOriginal setup.\n",
            "agents/poteto-agent.md": b"---\nname: poteto-agent\n---\nOriginal agent.\n",
            "docs/guide/README.md": b"Original reference material.\n",
        }
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name, data in self.files.items():
                entry = tarfile.TarInfo("archive/pstack/" + name)
                entry.size = len(data)
                bundle.addfile(entry, io.BytesIO(data))
        entries = {name: ("100644", _git_hash("blob", body)) for name, body in self.files.items()}
        self.source = Source("cursor/plugins", "a" * 40, "pstack", _tree_hash(entries))
        self.enterContext(patch("factory.lauren_native.SOURCE", self.source))

    def test_stages_the_original_cursor_layout_and_reopens_without_network(self):
        profile = lauren_native.provision(self.root, archive=self.archive)
        self.assertEqual(profile.revision, self.source.revision)
        self.assertEqual(profile.plugin_root, profile.home / ".cursor/plugins/local/pstack")
        self.assertEqual(profile.source, self.source)
        self.assertEqual(set(profile.skills), {"setup-pstack", "poteto-mode"})
        self.assertEqual(set(profile.agents), {"poteto-agent"})
        for relative, content in self.files.items():
            self.assertEqual((profile.plugin_root / relative).read_bytes(), content)
        with patch("factory.lauren_native.subprocess.run") as download:
            self.assertEqual(lauren_native.provision(self.root, pinned=self.source.revision), profile)
            download.assert_not_called()

    def test_tampered_bundle_cannot_be_reopened(self):
        profile = lauren_native.provision(self.root, archive=self.archive)
        profile.skills["poteto-mode"].write_text("Modified pstack", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected or changed"):
            lauren_native.provision(self.root, pinned=self.source.revision)

    def test_deep_windows_workspace_preserves_pinned_files(self):
        deep = self.root / ("workspace-" + "x" * 100) / ("profile-" + "y" * 55)
        profile = lauren_native.provision(deep, archive=self.archive)
        self.assertEqual(lauren_native.provision(deep, pinned=self.source.revision), profile)
        self.assertEqual(set(profile.skills), {"setup-pstack", "poteto-mode"})

    def test_profile_root_normalizes_parent_segments_before_checking_reparse_points(self):
        profile = lauren_native.provision(self.root / "unused" / "..", archive=self.archive)
        self.assertEqual(profile, lauren_native.provision(self.root, pinned=self.source.revision))

    def test_unknown_revision_is_rejected_before_provision(self):
        with self.assertRaisesRegex(ValueError, "No reviewed"):
            lauren_native.provision(self.root, pinned="b" * 40, archive=self.archive)
        self.assertFalse(self.root.exists())

    def test_profile_environment_is_isolated_and_has_no_inherited_cursor_key(self):
        profile = lauren_native.provision(self.root, archive=self.archive)
        existing = {"UserProfile": "outside", "CURSOR_CONFIG_DIR": "outside",
                    "LOCALAPPDATA": "outside", "cursor_api_key": "outside", "PATH": "kept",
                    "LINEAR_API_KEY": "outside", "OPENAI_API_KEY": "outside",
                    "ANTHROPIC_API_KEY": "outside", "GH_TOKEN": "outside"}
        env = lauren_native.cursor_environment(profile, existing)
        self.assertEqual(env["USERPROFILE"], str(profile.home))
        self.assertEqual(env["CURSOR_CONFIG_DIR"], str(profile.home / ".cursor"))
        self.assertEqual(env["LOCALAPPDATA"], str(profile.home / "AppData/Local"))
        self.assertEqual(env["PATH"], "kept")
        self.assertNotIn("cursor_api_key", env)
        self.assertNotIn("UserProfile", env)
        self.assertFalse({"LINEAR_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN"} & env.keys())
        self.assertEqual(existing["UserProfile"], "outside")

    def test_sync_checks_the_native_cursor_tree_without_advancing_any_pin(self):
        current = lauren_native.provision(self.root, archive=self.archive)
        root_hash, next_hash, next_tree = "d" * 40, "e" * 40, "f" * 40
        responses = [
            {"sha": next_hash, "commit": {"tree": {"sha": root_hash}}},
            {"truncated": False, "tree": [{"path": "pstack", "type": "tree", "sha": next_tree},
                                          {"path": "harness", "type": "tree", "sha": "9" * 40}]},
        ]
        with patch("factory.lauren_native._github", side_effect=responses) as api:
            report = lauren_native.sync(self.root)
        self.assertEqual(report["status"], "pending_validation")
        self.assertEqual(report["candidate_subtree"], "pstack")
        self.assertEqual(report["candidate_tree"], next_tree)
        self.assertEqual(report["revision"], current.revision)
        self.assertEqual(report["candidate_revision"], next_hash)
        self.assertEqual([call.args[0] for call in api.call_args_list],
                         ["repos/cursor/plugins/commits/HEAD", f"repos/cursor/plugins/git/trees/{root_hash}"])
        self.assertEqual(lauren_native.provision(self.root, pinned=current.revision), current)
