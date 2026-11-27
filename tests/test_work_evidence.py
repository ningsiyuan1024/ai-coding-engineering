import hashlib
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from factory.work_evidence import capture


class WorkEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "task"
        self.run_dir = Path(temp.name) / "run"
        self.repo.mkdir()
        self.run_dir.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        for name in ("committed.txt", "staged.txt", "dirty.txt", "remove", "before"):
            (self.repo / name).write_text("baseline-" + name + "\n")
        (self.repo / ".gitignore").write_text("ignored/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       encoding="utf-8").strip()

    def capture(self):
        artifacts = capture(self.repo, self.base, self.run_dir)
        files = {}
        for path, kind, digest in artifacts:
            self.assertTrue(path.is_relative_to(self.run_dir))
            data = path.read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            files[path.name] = data
        return json.loads(files.pop("worktree.json")), files

    def test_three_layers_deletion_rename_and_untracked_inventory(self):
        (self.repo / "committed.txt").write_text("committed improvement\n")
        self.git("commit", "-qam", "work")
        self.git("mv", "before", "after")
        (self.repo / "staged.txt").write_text("staged improvement\n")
        (self.repo / "added.txt").write_text("new staged contents\n")
        self.git("add", "staged.txt", "added.txt")
        (self.repo / "dirty.txt").write_text("unstaged improvement\n")
        (self.repo / "remove").unlink()
        (self.repo / "new").write_text("untracked contents must stay private")
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "cache").write_text("ignored")
        status = self.git("status", "--porcelain")
        manifest, patches = self.capture()
        self.assertEqual(manifest["base"], self.base)
        self.assertEqual(manifest["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(manifest["untracked_paths"], ["new"])
        self.assertIn(b"+committed improvement", patches["committed.patch"])
        self.assertIn(b"+staged improvement", patches["staged.patch"])
        self.assertIn(b"+new staged contents", patches["staged.patch"])
        self.assertIn(b"rename from before", patches["staged.patch"])
        self.assertIn(b"+unstaged improvement", patches["unstaged.patch"])
        self.assertIn(b"deleted file mode", patches["unstaged.patch"])
        self.assertNotIn(b"untracked contents", b"".join(patches.values()))
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual((manifest, patches), self.capture())

    def test_secrets_and_binary_files_have_no_patch_payload(self):
        bad = "ghp_" + "x" * 30
        (self.repo / "dirty.txt").write_text(bad + "\n")
        (self.repo / ".env").write_text("internal endpoint\n")
        (self.repo / "binary.bin").write_bytes(b"\0unexportable\0")
        (self.repo / bad).touch()
        self.git("add", ".env", "binary.bin")
        manifest, patches = self.capture()
        self.assertNotIn(bad.encode(), b"".join(patches.values()))
        self.assertNotIn(bad, str(manifest))
        self.assertNotIn(b"internal endpoint", b"".join(patches.values()))
        self.assertNotIn(b"unexportable", b"".join(patches.values()))
        omissions = {e["path"]: e.get("content_omitted") for entries in manifest["layers"].values()
                     for e in entries}
        self.assertEqual(omissions["dirty.txt"], "possible credential")
        self.assertEqual(omissions[".env"], "sensitive filename")
        self.assertEqual(omissions["binary.bin"], "binary data")

    def test_link_or_junction_is_never_traversed(self):
        external = self.run_dir / "outside"
        external.mkdir()
        (external / "outside-name-must-not-appear").write_text("outside-body-must-not-appear")
        (external / "inside.txt").write_text("outside-body-must-not-appear")
        link = self.repo / "redirect"
        link.mkdir()
        (link / "inside.txt").write_text("tracked file\n")
        self.git("add", "redirect/inside.txt")
        self.git("commit", "-qm", "tracked directory")
        (link / "inside.txt").unlink()
        link.rmdir()
        if os.name == "nt":
            # mklink is the Windows-native junction creator; unlink the junction
            # explicitly before unittest removes the temporary tree.
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external)],
                           check=True, capture_output=True)
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(external, target_is_directory=True)
        manifest, patches = self.capture()
        self.assertIn("redirect", manifest["untracked_paths"])
        self.assertNotIn("outside-name-must-not-appear", str(manifest))
        self.assertNotIn(b"outside-body-must-not-appear", b"".join(patches.values()))
        self.assertIn({"path": "redirect", "status": "uninspected", "content_omitted": "link or reparse point"},
                      manifest["layers"]["unstaged"])

    def test_retry_rejects_tampered_patch(self):
        self.capture()
        (self.run_dir / "worktree-evidence" / "staged.patch").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.capture()
