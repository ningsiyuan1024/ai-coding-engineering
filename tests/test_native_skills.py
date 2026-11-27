import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from factory.native_skills import SOURCES, Source, check_upstream, open_pinned, provision, sync


class NativeProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        manifest = {"name": "fixture", "version": "1", "skills": ["./skills/selected"]}
        self.files = {
            ".claude-plugin/plugin.json": json.dumps(manifest).encode(),
            "skills/selected/SKILL.md": b"upstream byte sequence\r\n",
            "skills/selected/reference.txt": b"dependency",
            "skills/unlisted/SKILL.md": b"not exported by plugin",
        }
        for name, body in self.files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "add", "."], check=True)
        tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
        source = Source("test/repo", "a" * 40, "upstream/plugin", tree)
        pins = patch.dict(SOURCES, {"example": source, "other": source})
        pins.start()
        self.addCleanup(pins.stop)
        self.archive = self.root / "source.tar.gz"
        self.write_archive()
        self.profile = self.root / "profile"

    def write_archive(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, body in self.files.items():
                member = tarfile.TarInfo("repo-123/upstream/plugin/" + name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))

    def test_concurrent_provision_uses_manifest_and_retains_dependencies(self):
        def install(_):
            return provision("example", self.profile, archive=self.archive)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.map(install, range(2))
        self.assertEqual(first, second)
        self.assertEqual(set(first.skills), {"selected"})
        self.assertEqual(first.skills["selected"].read_bytes(), self.files["skills/selected/SKILL.md"])
        self.assertEqual((first.skills["selected"].parent / "reference.txt").read_bytes(), b"dependency")
        with self.assertRaisesRegex(ValueError, "another production line"):
            provision("other", self.profile, archive=self.archive)
        self.assertEqual(first, provision("example", self.profile))

    def test_wrong_archive_fails_before_publish_then_retries_cleanly(self):
        self.files["skills/selected/SKILL.md"] = b"unexpected"
        self.write_archive()
        with self.assertRaisesRegex(ValueError, "Git tree mismatch"):
            provision("example", self.profile, archive=self.archive)
        self.assertFalse((self.profile / "native/example").exists())
        self.files["skills/selected/SKILL.md"] = b"upstream byte sequence\r\n"
        self.write_archive()
        self.assertEqual(provision("example", self.profile, archive=self.archive).plugin_name, "fixture")

    def test_reuse_rejects_extra_and_changed_content(self):
        bundle = provision("example", self.profile, archive=self.archive)
        extra = bundle.plugin_root / ".mcp.json"
        extra.write_text("{}")
        with self.assertRaisesRegex(ValueError, "Unexpected or changed"):
            provision("example", self.profile)
        extra.unlink()
        bundle.skills["selected"].write_bytes(b"edited")
        with self.assertRaisesRegex(ValueError, "Unexpected or changed"):
            provision("example", self.profile)

    def test_update_changes_new_tasks_and_retains_old_task_pin(self):
        original = provision("example", self.profile, archive=self.archive)
        name = "skills/selected/SKILL.md"
        self.files[name] = b"new upstream content"
        repo = self.root / "repo"
        (repo / name).write_bytes(self.files[name])
        subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "add", "."], check=True)
        tree = subprocess.check_output(["git", "-C", str(repo), "write-tree"], text=True).strip()
        candidate = Source("test/repo", "b" * 40, "upstream/plugin", tree)
        with patch("factory.native_skills.check_upstream", return_value=candidate):
            with self.assertRaisesRegex(ValueError, "Git tree mismatch"):
                sync("example", self.profile, archive=self.archive)
            self.assertEqual(provision("example", self.profile).revision, original.revision)
            self.write_archive()
            updated = sync("example", self.profile, archive=self.archive)
        self.assertEqual(updated.skills["selected"].read_bytes(), self.files[name])
        self.assertEqual(provision("example", self.profile).revision, candidate.revision)
        with patch.dict(SOURCES, {"example": candidate}):
            reopened = open_pinned("example", self.profile, original.revision, expected_tree=original.source.tree)
        self.assertEqual(reopened.skills["selected"].read_bytes(), b"upstream byte sequence\r\n")
        with self.assertRaisesRegex(ValueError, "Task pin"):
            open_pinned("example", self.profile, original.revision, expected_tree=candidate.tree)

    def test_check_upstream_resolves_subtree_from_the_same_commit(self):
        replies = [
            {"sha": "b" * 40, "commit": {"tree": {"sha": "c" * 40}}},
            {"tree": [{"path": "upstream", "type": "tree", "sha": "d" * 40}]},
            {"tree": [{"path": "plugin", "type": "tree", "sha": "e" * 40}]},
        ]
        with patch("factory.native_skills._github", side_effect=replies) as github:
            source = check_upstream("example")
        self.assertEqual(source, Source("test/repo", "b" * 40, "upstream/plugin", "e" * 40))
        self.assertEqual([call.args[0] for call in github.call_args_list], [
            "repos/test/repo/commits/HEAD", "repos/test/repo/git/trees/" + "c" * 40,
            "repos/test/repo/git/trees/" + "d" * 40,
        ])


if __name__ == "__main__":
    unittest.main()
