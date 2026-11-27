import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import uuid

from factory.runtime import (ClaudeRuntime, CodexRuntime, NativeQuestion, ProviderCredential,
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

    def claude(self):
        plugin = self.root / "plugin"
        manifest = plugin / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "upstream"}))
        with patch("factory.runtime._cli", return_value=["claude"]):
            r = ClaudeRuntime(self.home, self.work, plugin)
        return r

    def test_missing_profile_auth_never_runs_claude(self):
        r = self.claude()
        r.capabilities = lambda: {"authenticated": False}
        with patch("factory.runtime._Stream") as stream, self.assertRaises(RuntimeUnavailable):
            r.run("test", "sonnet", "high")
        stream.assert_not_called()

    def test_loading_is_distinct_from_skill_tool_invocation(self):
        r = self.claude()
        r.capabilities = lambda: {"authenticated": True}
        session = str(uuid.uuid4())
        observe = MagicMock()
        init = {"type": "system", "subtype": "init", "session_id": session,
                "plugins": [{"path": str(r.plugin), "name": "upstream"}], "skills": ["upstream:test"]}
        assistant = {"type": "assistant", "message": {"content": [{"type": "tool_use",
                     "name": "Skill", "input": {"skill": "upstream:test", "args": "private input"}}]}}
        request = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call",
                   "name": "Agent", "input": {"prompt": "private task"}}]}}
        reply = {"type": "assistant", "parent_tool_use_id": "call", "message": {"content": []}}
        tool_result = {"type": "user", "message": {"content": [{"type": "tool_result",
                       "tool_use_id": "call", "is_error": False}]}}
        result = {"type": "result", "subtype": "success", "uuid": "result-1", "result": "done",
                  "total_cost_usd": 0.012}
        with patch("factory.runtime._Stream") as stream:
            stream.return_value.next.side_effect = [init, assistant, request, reply, tool_result, result]
            with patch("factory.runtime.uuid.uuid4", return_value=uuid.UUID(session)):
                out = r.run("prompt", "sonnet", "high", on_event=observe)
        self.assertEqual(out.thread_id, session)
        self.assertEqual(out.text, "done")
        observe.assert_any_call("runtime.plugin", {"plugin.name": "upstream", "skill.count": 1})
        observe.assert_any_call("runtime.tool", {"tool.name": "Skill", "skill.name": "upstream:test"})
        observe.assert_any_call("runtime.cost", {"cost.estimated_usd": 0.012})
        observe.assert_any_call("runtime.subagent.request", {"tool.name": "Agent"})
        observe.assert_any_call("runtime.subagent.activity", {"activity.kind": "assistant_reply"})
        observe.assert_any_call("runtime.subagent.tool_result", {"tool.error": False})
        self.assertNotIn("private input", str(observe.call_args_list))
        self.assertNotIn("private task", str(observe.call_args_list))

    def test_cross_profile_plugin_prevents_success(self):
        r = self.claude()
        r.capabilities = lambda: {"authenticated": True}
        session = str(uuid.uuid4())
        with patch("factory.runtime._Stream") as stream:
            stream.return_value.next.return_value = {
                "type": "system", "subtype": "init", "session_id": session,
                "plugins": [{"name": "upstream", "path": str(self.root / "wrong")}],
            }
            with self.assertRaisesRegex(RuntimeUnavailable, "unexpected"):
                r.run("prompt", "sonnet", "low", session)

    def test_only_opted_in_credential_can_reach_matching_provider(self):
        sample = {"ANTHROPIC_BASE_URL": "https://relay.invalid", "ANTHROPIC_API_KEY": "relay",
                  "GH_TOKEN": "write", "OSS_LINEAR_API_KEY": "tracker", "FACTORY_LINE_KEY": "official"}
        with patch.dict(os.environ, sample):
            private = _environment(self.home, "CLAUDE_CONFIG_DIR")
            self.assertTrue(set(sample).isdisjoint(private))
            self.assertEqual(private["GIT_CONFIG_VALUE_0"], "")
            wrong = ProviderCredential("FACTORY_LINE_KEY", "ANTHROPIC_API_KEY", "https://relay.invalid")
            with self.assertRaises(ValueError):
                _environment(self.home, "CLAUDE_CONFIG_DIR", wrong)
            auth = ProviderCredential("FACTORY_LINE_KEY", "ANTHROPIC_API_KEY", "https://api.anthropic.com")
            bound = _environment(self.home, "CLAUDE_CONFIG_DIR", auth)
            self.assertEqual(bound["ANTHROPIC_API_KEY"], "official")
            self.assertEqual(bound["ANTHROPIC_BASE_URL"], auth.base_url)
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

    def test_claude_human_question_preserves_the_bound_session(self):
        r = self.claude()
        r.capabilities = lambda: {"authenticated": True}
        session = str(uuid.uuid4())
        bound = MagicMock()
        init = {"type": "system", "subtype": "init", "session_id": session,
                "plugins": [{"name": "upstream", "path": str(r.plugin)}]}
        question = {"type": "assistant", "message": {"content": [{"type": "tool_use",
                    "name": "AskUserQuestion", "input": {"questions": [{"question": "Which target?"}]}}]}}
        with patch("factory.runtime._Stream") as stream:
            stream.return_value.next.side_effect = [init, question]
            with self.assertRaisesRegex(NativeQuestion, "Which target"):
                r.run("test", "sonnet", "high", thread_id=session, on_thread=bound)
            args = stream.call_args.args[0]
            denials = json.loads(args[args.index("--settings") + 1])["permissions"]["deny"]
            self.assertIn("Bash(git push:*)", denials)
            self.assertIn("Bash(gh pr create:*)", denials)
        bound.assert_called_once_with(session)
