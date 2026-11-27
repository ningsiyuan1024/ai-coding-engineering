import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from factory.runtime import (CodexRuntime, NativeQuestion, ProviderCredential,
                             RuntimeUnavailable, _environment, prepare_worktree)


class WorktreeTest(unittest.TestCase):
    def test_retry_reuses_an_unattached_task_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            repo, work = parent / "repo", parent / "work"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                            "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m",
                            "baseline"], check=True)
            key = hashlib.sha256(os.path.normcase(str(work.resolve())).encode()).hexdigest()[:24]
            subprocess.run(["git", "-C", str(repo), "branch", f"factory/{key}"], check=True)
            revision = prepare_worktree(repo, work, "HEAD")
            self.assertEqual(revision, prepare_worktree(repo, work, "HEAD"))

    def test_reentry_preserves_dirty_files_and_rejects_another_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            repo, other, work = parent / "origin", parent / "other", parent / "task"
            for repository in (repo, other):
                subprocess.run(["git", "init", "-q", str(repository)], check=True)
                subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test",
                                "-c", "user.email=test@example.invalid", "commit",
                                "-q", "--allow-empty", "-m", "baseline"], check=True)
            revision = prepare_worktree(repo, work, "HEAD")
            (work / "result").write_text("work in progress")
            self.assertEqual(revision, prepare_worktree(repo, work, revision))
            self.assertEqual((work / "result").read_text(), "work in progress")
            with self.assertRaises(ValueError):
                prepare_worktree(other, work, "HEAD")


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home, self.work = self.root / "profile", self.root / "work"
        self.work.mkdir()

    def test_unavailable_model_never_creates_session(self):
        with patch("factory.runtime._cli", return_value=["codex"]):
            r = CodexRuntime(self.home, self.work)
        r.capabilities = lambda: {"models": {"gpt-6.2-astra": ["xhigh"]}}
        r._rpc = MagicMock()
        with self.assertRaisesRegex(RuntimeUnavailable, "does not advertise"):
            r.run("test", "gpt-6.2-astra", "ultra")
        r._rpc.assert_not_called()

    def test_foreign_discovery_fails_closed_after_ineffective_disable(self):
        with patch("factory.runtime._cli", return_value=["codex"]):
            r = CodexRuntime(self.home, self.work)
        path = str(self.root / "global" / "SKILL.md")
        r.skills = lambda: [{"path": path, "enabled": True}]
        r._rpc = MagicMock()
        with self.assertRaisesRegex(RuntimeUnavailable, "Foreign skills"):
            r.isolate_skills()
        r._rpc.assert_called_once_with("skills/config/write", {"path": path, "enabled": False})

    def test_repository_guidance_can_be_read_without_activating_its_skills(self):
        with patch("factory.runtime._cli", return_value=["codex"]):
            rt = CodexRuntime(self.home, self.work)
        project = {"enabled": True, "path": str(self.work / ".agents/skills/project/SKILL.md")}
        pinned = {"enabled": True, "path": str(self.home / "skills/owned/SKILL.md")}
        rt.skills = MagicMock(side_effect=[[pinned, project], [pinned, {**project, "enabled": False}]])
        rt._rpc = MagicMock()
        self.assertEqual(rt.isolate_skills(), [pinned])
        self.assertTrue(rt._trusted(self.work / "AGENTS.md"))
        rt._rpc.assert_called_once_with("skills/config/write", {"path": project["path"], "enabled": False})


    def test_only_opted_in_credential_can_reach_matching_provider(self):
        sample = {"ANTHROPIC_BASE_URL": "https://relay.invalid", "ANTHROPIC_API_KEY": "relay",
                  "GH_TOKEN": "write", "OSS_LINEAR_API_KEY": "tracker", "FACTORY_LINE_KEY": "official"}
        with patch.dict(os.environ, sample):
            private = _environment(self.home, "CODEX_HOME")
            self.assertTrue(set(sample).isdisjoint(private))
            self.assertEqual(private["GIT_CONFIG_VALUE_0"], "")
            wrong = ProviderCredential("FACTORY_LINE_KEY", "ANTHROPIC_API_KEY", "https://relay.invalid")
            with self.assertRaises(ValueError):
                _environment(self.home, "CODEX_HOME", wrong)
            auth = ProviderCredential("FACTORY_LINE_KEY", "OPENAI_API_KEY", "https://api.openai.com/v1")
            bound = _environment(self.home, "CODEX_HOME", auth)
            self.assertEqual(bound["OPENAI_API_KEY"], "official")
            self.assertEqual(bound["OPENAI_BASE_URL"], auth.base_url)
            self.assertNotIn("FACTORY_LINE_KEY", bound)

    def test_codex_human_question_is_not_an_answer_or_approval(self):
        with patch("factory.runtime._cli", return_value=["codex"]):
            r = CodexRuntime(self.home, self.work)
        r.running_thread = "thread"
        r.stream = MagicMock()
        r.stream.next.return_value = {"method": "item/tool/requestUserInput", "id": 7,
            "params": {"threadId": "thread", "turnId": "turn", "questions": [
                {"question": "Which revision?", "options": [{"label": "A"}, {"label": "B"}]}]}}
        with self.assertRaises(NativeQuestion) as captured:
            r._packet(5)
        self.assertEqual(captured.exception.text, "Which revision?\n- A\n- B")
        self.assertIn("error", r.stream.send.call_args.args[0])
        self.assertEqual(r.active, ("thread", "turn"))

    def test_skill_and_collaboration_evidence_uses_only_native_structured_events(self):
        with patch("factory.runtime._cli", return_value=["codex"]):
            rt = CodexRuntime(self.home, self.work)
        skill = self.home / "skills" / "tdd" / "SKILL.md"
        rt.skill_paths = {skill.resolve(): "mattpocock-skills:tdd"}
        emit = MagicMock()
        read = {"type":"commandExecution", "status":"completed", "exitCode":0,
                "commandActions":[{"type":"read", "path":str(skill), "command":"private contents"}]}
        rt.observe_tool(read, emit)
        rt.observe_tool({**read, "exitCode":1}, emit)
        rt.observe_tool({**read, "commandActions":[{"type":"read", "path":"other/SKILL.md"}]}, emit)
        rt.observe_tool({"type":"collabAgentToolCall", "tool":"spawnAgent", "prompt":"private task",
                         "receiverThreadIds":["child"]}, emit)
        self.assertEqual([call.args for call in emit.call_args_list], [
            ("runtime.skill_read", {"skill.name":"mattpocock-skills:tdd"}),
            ("runtime.subagent", {"operation":"spawnAgent", "receiver.count":1}),
        ])
