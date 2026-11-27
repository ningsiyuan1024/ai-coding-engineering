"""Thin native session adapters. Profile homes are private to a production line."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass


class RuntimeUnavailable(RuntimeError):
    """An executable, capability, or profile prerequisite is unavailable."""


class NativeQuestion(RuntimeError):
    """A native question ended the turn. Resume its thread with the human's decision."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


def _question_text(data):
    questions = []
    for item in data.get("questions", []):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            continue
        text = item["question"].strip()
        if not text:
            continue
        options = [o["label"] for o in item.get("options") or []
                   if isinstance(o, dict) and isinstance(o.get("label"), str)]
        questions.append("\n".join([text, *(f"- {label}" for label in options)]))
    return "\n\n".join(questions)


def _git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
    ).stdout.strip()


def prepare_worktree(repo, worktree, base):
    """Create/re-enter a linked worktree on its task branch, pinned to base."""
    repo, path = Path(repo).resolve(strict=True), Path(worktree).resolve()
    revision = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    key = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]
    branch = f"factory/{key}"
    if not path.exists() or (path.is_dir() and not any(path.iterdir())):
        path.parent.mkdir(parents=True, exist_ok=True)
        present = subprocess.run(["git", "-C", str(repo), "show-ref", "--quiet", "--verify",
                                  f"refs/heads/{branch}"]).returncode
        if present not in (0, 1):
            raise RuntimeUnavailable("Cannot verify the reserved worktree branch")
        try:
            if present == 0:
                _git(repo, "merge-base", "--is-ancestor", revision, branch)
                _git(repo, "worktree", "add", str(path), branch)
            else:
                _git(repo, "worktree", "add", "-b", branch, str(path), revision)
        except subprocess.CalledProcessError:
            # Another identical claim may have completed the checkout concurrently.
            if not (path / ".git").is_file():
                raise
    common = lambda location: Path(_git(location, "rev-parse", "--path-format=absolute",
                                        "--git-common-dir")).resolve()
    if (Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path
            or common(repo) != common(path)
            or _git(path, "symbolic-ref", "--short", "HEAD") != branch):
        raise ValueError("Existing directory is not this task's linked worktree")
    _git(path, "merge-base", "--is-ancestor", revision, "HEAD")
    return revision


@dataclass(frozen=True)
class RunResult:
    thread_id: str
    turn_id: str | None
    text: str


def _ignore(*args):
    pass


def _cli(name, executable=None):
    path = shutil.which(str(executable)) if executable else None
    if not executable:
        if os.name == "nt":
            path = shutil.which(name + ".exe")
        path = path or shutil.which(name)
    if not path:
        raise RuntimeUnavailable(f"{name} is not installed")
    if os.name == "nt" and Path(path).suffix.lower() in {".ps1", ".cmd", ".bat", ""}:
        package, entry = {
            "codex": ("@openai/codex", "bin/codex.js"),
            "claude": ("@anthropic-ai/claude-code", "cli.js"),
        }[name]
        script = Path(path).parent / "node_modules" / package / entry
        node = shutil.which("node.exe")
        if not node or not script.is_file():
            raise RuntimeUnavailable(f"Cannot resolve native {name} executable from launcher")
        return [node, str(script)]
    return [path]


@dataclass(frozen=True)
class ProviderCredential:
    """Explicit binding from an operator's dedicated env variable to a first-party host."""

    source: str
    target: str
    base_url: str


def _environment(profile, variable, credential=None):
    host_home = Path.home().resolve()
    if profile in {host_home, host_home / ".codex", host_home / ".claude"}:
        raise ValueError("A dedicated production-line profile directory is required")
    profile.mkdir(parents=True, exist_ok=True)
    user_home = profile / "user-home"
    user_home.mkdir(exist_ok=True)
    # Engineering workers receive neither the coordinator's publication credentials
    # nor an ambient API relay paired with another provider's token.
    excluded_prefixes = ("ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_", "FACTORY_", "GH_", "GITHUB_",
                         "LINEAR_", "OSS_LINEAR_", "GIT_CONFIG_", "GCM_", "AZURE_",
                         "AWS_", "CLOUDSDK_", "GOOGLE_CLOUD_")
    excluded_keys = {"CLAUDECODE", "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK",
                     "GOOGLE_APPLICATION_CREDENTIALS", "GIT_SSH", "GIT_SSH_COMMAND"}
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(excluded_prefixes) and k.upper() not in excluded_keys
           and not re.search(r"(?:^|_)(TOKEN|PASSWORD|SECRET|CREDENTIALS|API_KEY)(?:_|$)", k, re.I)}
    if "CODEX_SANDBOX_NETWORK_DISABLED" in os.environ:
        env["CODEX_SANDBOX_NETWORK_DISABLED"] = os.environ["CODEX_SANDBOX_NETWORK_DISABLED"]
    env.update({variable: str(profile), "HOME": str(user_home), "USERPROFILE": str(user_home),
                "XDG_CONFIG_HOME": str(user_home / ".config"),
                "GH_CONFIG_DIR": str(user_home / ".config" / "gh"), "GH_PROMPT_DISABLED": "1",
                "GIT_CONFIG_GLOBAL": str(user_home / ".gitconfig"), "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "", "GCM_INTERACTIVE": "never"})
    if credential is not None:
        is_codex = variable == "CODEX_HOME"
        origin = "https://api.openai.com/v1" if is_codex else "https://api.anthropic.com"
        permitted = {"OPENAI_API_KEY"} if is_codex else {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
        if (not re.fullmatch(r"FACTORY_[A-Z0-9_]+", credential.source)
                or credential.target not in permitted or credential.base_url.rstrip("/") != origin):
            raise ValueError("Credential requires a dedicated source and its matching official endpoint")
        if not os.environ.get(credential.source):
            raise RuntimeUnavailable("No credential configured in the selected environment binding")
        if is_codex:
            import tomllib
            config_file = profile / "config.toml"
            config = tomllib.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
            if (config.get("model_provider", "openai") != "openai"
                    or config.get("openai_base_url", origin).rstrip("/") != origin
                    or "openai" in config.get("model_providers", {})):
                raise ValueError("Codex profile has routing overrides incompatible with the credential binding")
        else:
            for filename in ("settings.json", "settings.local.json"):
                settings_file = profile / filename
                if settings_file.exists():
                    config = json.loads(settings_file.read_text(encoding="utf-8"))
                    overrides = {k.upper() for k in config.get("env", {})}
                    if ("apiKeyHelper" in config or any(k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
                                                       for k in overrides)):
                        raise ValueError("Claude profile has routing overrides incompatible with the credential binding")
        env.pop(credential.source, None)
        env[credential.target] = os.environ[credential.source]
        env["OPENAI_BASE_URL" if is_codex else "ANTHROPIC_BASE_URL"] = origin
    return env


class _Stream:
    """Bounded-time reads; the subprocess remains owned by the calling adapter."""

    def __init__(self, command, cwd, env):
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def send(self, obj):
        self.process.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next(self, deadline):
        try:
            line = self.lines.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise TimeoutError("Native runtime response timed out") from exc
        if line is None:
            raise RuntimeUnavailable(f"Native runtime ended (exit {self.process.poll()})")
        return json.loads(line)

    def close(self):
        if not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()


class CodexRuntime:
    def __init__(self, profile_home, worktree, executable=None, timeout=900, read_only=False,
                 credential=None):
        self.profile = Path(profile_home).resolve()
        self.cwd = Path(worktree).resolve(strict=True)
        self.command = _cli("codex", executable)
        self.env = _environment(self.profile, "CODEX_HOME", credential)
        if credential is not None:
            self.command.extend(["-c", 'model_provider="openai"', "-c",
                                 'openai_base_url="https://api.openai.com/v1"'])
        self.timeout = timeout
        self.read_only = read_only
        self.stream = None
        self.counter = 0
        self.active = None
        self.running_thread = None
        self.sink = _ignore

    def __enter__(self):
        self.stream = _Stream(self.command + ["app-server", "--listen", "stdio://"],
                              self.cwd, self.env)
        try:
            init = self._rpc("initialize", {"clientInfo": {"name": "software_factory",
                             "version": "0.1"}, "capabilities": {"experimentalApi": True}})
            if Path(init["codexHome"]).resolve() != self.profile:
                raise RuntimeUnavailable("App-server ignored the requested profile")
            self.version = init["userAgent"]
            self.stream.send({"method": "initialized"})
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self, *exc):
        self.stream.close()

    def _packet(self, deadline):
        packet = self.stream.next(deadline)
        if "method" in packet and "id" in packet:
            # Return a terminal error to unsupported interactive/dynamic tool requests.
            self.stream.send({"id": packet["id"], "error": {"code": -32601,
                              "message": "Interactive request requires a human session"}})
            params = packet.get("params", {})
            if (packet["method"] == "item/tool/requestUserInput" and self.running_thread
                    and params.get("threadId") == self.running_thread):
                question = _question_text(params)
                if question:
                    if params.get("turnId"):
                        self.active = (self.running_thread, params["turnId"])
                    raise NativeQuestion(question)
        elif "method" in packet:
            self.sink(packet["method"], packet.get("params", {}))
        return packet

    def _rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.stream.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + min(self.timeout, 90)
        while True:
            packet = self._packet(deadline)
            if packet.get("id") == request_id and "method" not in packet:
                if "error" in packet:
                    raise RuntimeUnavailable(f"{method}: {packet['error'].get('message', 'RPC error')}")
                return packet["result"]

    def capabilities(self):
        models, cursor = {}, None
        while True:
            page = self._rpc("model/list", {"cursor": cursor, "limit": 100, "includeHidden": True})
            for m in page["data"]:
                models[m["model"]] = [r["reasoningEffort"] for r in m["supportedReasoningEfforts"]]
            cursor = page.get("nextCursor")
            if not cursor:
                break
        account = self._rpc("account/read", {})
        return {"models": models, "authenticated": bool(account.get("account")),
                "requires_auth": account.get("requiresOpenaiAuth"), "version": self.version}

    def skills(self):
        result = self._rpc("skills/list", {"cwds": [str(self.cwd)], "forceReload": True})
        entries = result["data"]
        if len(entries) != 1 or entries[0].get("errors"):
            raise RuntimeUnavailable("Native skill discovery failed")
        return entries[0]["skills"]

    def _trusted(self, path):
        resolved = Path(path).resolve()
        return resolved.is_relative_to(self.profile) or resolved.is_relative_to(self.cwd)

    def isolate_skills(self):
        # Some Windows builds also scan the host .agents directory. Store exclusions
        # through Codex's profile-local configuration API and re-read effective state.
        for skill in self.skills():
            if skill["enabled"] and not Path(skill["path"]).resolve().is_relative_to(self.profile):
                self._rpc("skills/config/write", {"path": skill["path"], "enabled": False})
        enabled = [s for s in self.skills() if s["enabled"]]
        if any(not Path(s["path"]).resolve().is_relative_to(self.profile) for s in enabled):
            raise RuntimeUnavailable("Foreign skills are enabled in this production line")
        return enabled

    def open_session(self, model, effort, thread_id=None, *, require_auth=False):
        """Start or resume the native thread and verify its effective profile before any turn."""
        capabilities = self.capabilities()
        if effort not in capabilities["models"].get(model, []):
            raise RuntimeUnavailable(f"App-server does not advertise {model} with effort {effort}")
        self.isolate_skills()
        if require_auth and capabilities["requires_auth"] and not capabilities["authenticated"]:
            raise RuntimeUnavailable("Codex authentication required in this production-line profile")
        if thread_id:
            previous = self._rpc("thread/read", {"threadId": thread_id, "includeTurns": False})
            if Path(previous["thread"]["cwd"]).resolve() != self.cwd:
                raise ValueError("Native session belongs to a different worktree")
        sandbox = "read-only" if self.read_only else "workspace-write"
        config = {"model_reasoning_effort": effort}
        if os.name == "nt" and not self.read_only:
            config["windows.sandbox"] = "unelevated"
        params = {"cwd": str(self.cwd), "model": model, "config": config,
                  "sandbox": sandbox, "approvalPolicy": "never"}
        if thread_id:
            params["threadId"] = thread_id
            params["excludeTurns"] = True
        opened = self._rpc("thread/resume" if thread_id else "thread/start", params)
        expected_sandbox = "readOnly" if self.read_only else "workspaceWrite"
        instructions = [Path(p).resolve() for p in opened.get("instructionSources", [])]
        policy = opened["sandbox"]
        if (opened["model"] != model or opened.get("reasoningEffort") != effort
                or policy["type"] != expected_sandbox or policy.get("networkAccess") is True
                or any(not Path(p).resolve().is_relative_to(self.cwd)
                       for p in policy.get("writableRoots", []))
                or any(not self._trusted(p) for p in instructions)
                or ((self.profile / "AGENTS.md").is_file()
                    and self.profile / "AGENTS.md" not in instructions)):
            raise RuntimeUnavailable("Native session configuration did not match its profile")
        return {"thread_id": opened["thread"]["id"], "model": opened["model"],
                "effort": opened["reasoningEffort"], "sandbox": policy,
                "instructions": [str(p) for p in instructions]}

    def run(self, prompt, model, effort, thread_id=None, on_event=_ignore, on_thread=_ignore):
        opened = self.open_session(model, effort, thread_id, require_auth=True)
        session = opened["thread_id"]
        on_thread(session)
        completed, messages = {}, []

        def observe(name, params):
            if params.get("threadId") != session:
                return
            if name == "turn/completed":
                turn = params["turn"]
                completed[turn["id"]] = turn["status"]
            elif name == "item/completed":
                item = params["item"]
                kind = item["type"]
                if kind == "agentMessage":
                    if item.get("phase") != "commentary":
                        messages.append(item.get("text", ""))
                attrs = {"item.type": kind}
                for key in ("tool", "status", "model", "reasoningEffort", "agentThreadId"):
                    value = item.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,128}", value):
                        attrs[f"item.{key}"] = value
                for key in ("exitCode", "durationMs"):
                    if type(item.get(key)) is int:
                        attrs[f"item.{key}"] = item[key]
                on_event("runtime.item", attrs)
            elif name == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage", {}).get("last", {})
                on_event("runtime.usage", {f"usage.{k}": v for k, v in usage.items()
                                          if type(v) is int})

        self.sink = observe
        self.running_thread = session
        try:
            turn = self._rpc("turn/start", {"threadId": session, "model": model, "effort": effort,
                             "input": [{"type": "text", "text": prompt, "text_elements": []}]})
            turn_id = turn["turn"]["id"]
            self.active = (session, turn_id)
            deadline = time.monotonic() + self.timeout
            while turn_id not in completed:
                self._packet(deadline)
            on_event("runtime.turn", {"model": model, "effort": effort, "status": completed[turn_id]})
            if completed[turn_id] != "completed":
                raise RuntimeUnavailable(f"Codex turn ended with status {completed[turn_id]}")
            return RunResult(session, turn_id, messages[-1] if messages else "")
        except BaseException:
            if self.active and self.active[1] not in completed:
                try:
                    self.interrupt()
                except OSError:
                    pass  # The process may already have terminated with the original error.
            raise
        finally:
            self.active, self.running_thread, self.sink = None, None, _ignore

    def interrupt(self):
        if self.active:
            thread, turn = self.active
            self.stream.send({"id": -1, "method": "turn/interrupt",
                              "params": {"threadId": thread, "turnId": turn}})


class ClaudeRuntime:
    """Claude Code owns skill selection, subagents, tools, and transcripts."""

    # Native command permission rules cover normal CLI publication commands.
    # They do not constitute an operating-system network boundary for arbitrary code.
    _publication_rules = [
        "Bash(git push:*)", "Bash(git send-email:*)",
        *(f"Bash(gh pr {action}:*)" for action in
          ("create", "edit", "comment", "review", "merge", "close", "reopen", "ready")),
        *(f"Bash(gh issue {action}:*)" for action in
          ("create", "edit", "comment", "close", "reopen", "delete", "transfer")),
        "Bash(gh api *--method*)", "Bash(gh api *-X*)", "Bash(gh api *--input*)",
        "Bash(gh api *--field*)", "Bash(gh api *--raw-field*)", "Bash(gh api * -f*)",
        "Bash(gh api * -F*)", "Bash(gh release create:*)", "Bash(gh workflow run:*)",
    ]

    def __init__(self, profile_home, worktree, plugin_root, executable=None,
                 timeout=900, read_only=False, credential=None):
        self.profile = Path(profile_home).resolve()
        self.cwd = Path(worktree).resolve(strict=True)
        self.plugin = Path(plugin_root).resolve(strict=True)
        manifest = self.plugin / ".claude-plugin" / "plugin.json"
        self.plugin_name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
        self.command = _cli("claude", executable)
        self.env = _environment(self.profile, "CLAUDE_CONFIG_DIR", credential)
        self.timeout, self.read_only = timeout, read_only
        self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def capabilities(self):
        status = subprocess.run(self.command + ["auth", "status", "--json"], cwd=self.cwd,
                                env=self.env, capture_output=True, timeout=30, encoding="utf-8")
        try:
            account = json.loads(status.stdout)
        except ValueError as exc:
            raise RuntimeUnavailable("Could not read Claude profile authentication status") from exc
        return {"authenticated": account.get("loggedIn") is True, "requires_auth": True,
                "auth_method": account.get("authMethod")}

    def _host_args(self):
        settings = {"permissions": {"deny": self._publication_rules}}
        args = ["--plugin-dir", str(self.plugin), "--setting-sources", "user",
                "--permission-mode", "dontAsk" if self.read_only else "acceptEdits"]
        if self.read_only:
            # Read/plan turns exclude shell, hook and remote tool writes. Claude's
            # tool permissions do not create an OS sandbox on native Windows.
            args.extend(["--tools", "Read,Glob,Grep,Skill,AskUserQuestion", "--strict-mcp-config"])
            settings["disableAllHooks"] = True
        return args + ["--settings", json.dumps(settings)]

    def discover(self):
        """Read native commands and agents via the Claude SDK handshake, with no user turn."""
        args = ["--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", *self._host_args()]
        stream = _Stream(self.command + args, self.cwd, self.env)
        def query(subtype):
            request_id = str(uuid.uuid4())
            stream.send({"type": "control_request", "request_id": request_id,
                         "request": {"subtype": subtype}})
            deadline = time.monotonic() + min(self.timeout, 45)
            while True:
                packet = stream.next(deadline)
                response = packet.get("response", {})
                if packet.get("type") == "control_response" and response.get("request_id") == request_id:
                    if response.get("subtype") != "success":
                        raise RuntimeUnavailable(f"Claude SDK {subtype} failed")
                    return response["response"]
        try:
            metadata = query("initialize")
            effective = query("get_settings")["effective"]
            observed = effective.get("permissions", {}).get("deny", [])
            if not set(self._publication_rules).issubset(observed):
                raise RuntimeUnavailable("Claude did not apply the expected publication restrictions")
            return {"commands": [c["name"] for c in metadata["commands"]],
                    "agents": [a["name"] for a in metadata["agents"]],
                    "models": [m["value"] for m in metadata["models"]],
                    "model_efforts": {m["value"]: list(m.get("supportedEffortLevels", []))
                                      for m in metadata["models"] if m.get("supportsEffort") is True},
                    "publication_rules_observed": len(self._publication_rules),
                    "hooks_disabled": effective.get("disableAllHooks") is True}
        finally:
            stream.close()

    def run(self, prompt, model, effort, thread_id=None, on_event=_ignore, on_thread=_ignore):
        if effort not in {"low", "medium", "high", "max"}:
            raise RuntimeUnavailable(f"Unsupported Claude Code reasoning effort: {effort}")
        if not self.capabilities()["authenticated"]:
            raise RuntimeUnavailable("Claude authentication required in this production-line profile")
        session = str(uuid.UUID(thread_id)) if thread_id else str(uuid.uuid4())
        args = ["--print", "--output-format", "stream-json", "--verbose", "--model", model,
                "--effort", effort, "--resume" if thread_id else "--session-id", session,
                *self._host_args()]
        self.stream = _Stream(self.command + args, self.cwd, self.env)
        try:
            self.stream.process.stdin.write(prompt)
            self.stream.process.stdin.close()
            deadline = time.monotonic() + self.timeout
            initialized = False
            delegated = set()
            error_kind = None
            while True:
                event = self.stream.next(deadline)
                kind = event.get("type")
                if kind == "system" and event.get("subtype") == "init":
                    plugins = event.get("plugins", [])
                    if (event.get("session_id") != session or len(plugins) != 1
                            or plugins[0].get("name") != self.plugin_name
                            or Path(plugins[0].get("path", "")).resolve() != self.plugin):
                        raise RuntimeUnavailable("Claude loaded an unexpected session or plugin set")
                    initialized = True
                    on_thread(session)
                    on_event("runtime.plugin", {"plugin.name": self.plugin_name,
                             "skill.count": len(event.get("skills", []))})
                    resolved_model = event.get("model")
                    if isinstance(resolved_model, str) and re.fullmatch(r"[\w.:-]{1,128}", resolved_model):
                        on_event("runtime.model", {"model": resolved_model, "model.stage": "initialization"})
                elif kind == "assistant" and initialized:
                    message = event.get("message", {})
                    if event.get("error") in {"authentication_failed", "billing_error", "rate_limit",
                                              "invalid_request", "server_error", "unknown"}:
                        error_kind = event["error"]
                        on_event("runtime.error", {"error.kind": error_kind})
                    if event.get("parent_tool_use_id") in delegated:
                        on_event("runtime.subagent.activity", {"activity.kind": "assistant_reply"})
                    for block in message.get("content", []):
                        if block.get("type") != "tool_use":
                            continue
                        tool = block.get("name", "")
                        if re.fullmatch(r"[\w:.-]{1,128}", tool):
                            attrs = {"tool.name": tool}
                            if tool == "AskUserQuestion":
                                question = _question_text(block.get("input", {}))
                                if question:
                                    on_event("runtime.tool", attrs)
                                    self.interrupt()
                                    raise NativeQuestion(question)
                            if tool == "Skill":
                                skill = block.get("input", {}).get("skill", "")
                                if isinstance(skill, str) and re.fullmatch(r"[\w:.-]{1,128}", skill):
                                    attrs["skill.name"] = skill
                            if tool in {"Agent", "Task"} and isinstance(block.get("id"), str):
                                delegated.add(block["id"])
                                on_event("runtime.subagent.request", {"tool.name": tool})
                            on_event("runtime.tool", attrs)
                elif kind == "user" and initialized:
                    for block in event.get("message", {}).get("content", []):
                        if (isinstance(block, dict) and block.get("type") == "tool_result"
                                and block.get("tool_use_id") in delegated):
                            on_event("runtime.subagent.tool_result",
                                     {"tool.error": block.get("is_error") is True})
                elif kind == "result":
                    usage = event.get("usage", {})
                    on_event("runtime.usage", {f"usage.{k}": v for k, v in usage.items()
                                              if type(v) is int})
                    estimate = event.get("total_cost_usd")
                    if type(estimate) in (int, float) and math.isfinite(estimate) and estimate >= 0:
                        on_event("runtime.cost", {"cost.estimated_usd": estimate})
                    for observed_model in event.get("modelUsage", {}):
                        if re.fullmatch(r"[\w.:-]{1,128}", observed_model):
                            on_event("runtime.model", {"model": observed_model, "model.stage": "usage"})
                    failed = not initialized or event.get("is_error") or event.get("subtype") != "success"
                    attrs = {"status": "failed" if failed else "completed"}
                    if isinstance(event.get("permission_denials"), list):
                        attrs["permission.denials"] = len(event["permission_denials"])
                    on_event("runtime.turn", attrs)
                    if failed:
                        raise RuntimeUnavailable(f"Claude turn failed ({error_kind or 'native result error'})")
                    return RunResult(session, event.get("uuid"), event.get("result", ""))
        finally:
            self.stream.close()
            self.stream = None

    def interrupt(self):
        if self.stream and self.stream.process.poll() is None:
            self.stream.process.terminate()
