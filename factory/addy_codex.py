"""Install Addy Osmani's fixed local marketplace using the upstream Codex entrypoint."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tomllib

from .native_skills import open_pinned, verify_same_files
from .runtime import CodexRuntime, RuntimeUnavailable


PLUGIN = "agent-skills@agent-skills"


def _catalog(runtime, source, revision):
    catalog = runtime._rpc("plugin/list", {"marketplaceKinds": ["local"], "forceRefetch": False,
                                           "cwds": [str(runtime.cwd)]})
    result = {"installed": False, "enabled": False}
    for market in catalog["marketplaces"]:
        for plugin in market["plugins"]:
            if plugin["id"] != PLUGIN and plugin.get("enabled") is True:
                raise RuntimeUnavailable("Another plugin is enabled in the Addy Codex profile")
            if plugin["id"] == PLUGIN:
                origin = plugin.get("source", {})
                if origin.get("type") != "local":
                    raise RuntimeUnavailable("Addy's marketplace name points to a different source")
                registered = Path(origin["path"]).resolve(strict=True)
                if not os.path.samefile(registered, source.plugin_root):
                    mirror = _source(registered, revision)
                    if mirror.source != source.source:
                        raise RuntimeUnavailable("Addy's registered marketplace differs from the task pin")
                result = {"installed": plugin["installed"], "enabled": plugin["enabled"]}
    return result


def _command(runtime, args):
    result = subprocess.run(runtime.command + ["plugin", *args, "--json"], cwd=runtime.cwd,
                            env={**runtime.env, "GIT_ALLOW_PROTOCOL": "file"}, capture_output=True,
                            encoding="utf-8", stdin=subprocess.DEVNULL, timeout=90)
    if result.returncode:
        raise RuntimeUnavailable("Codex rejected the local Addy marketplace or plugin")
    return json.loads(result.stdout)


def _source(root, revision):
    if (root.parent.name != revision or root.name != "plugin" or root.parents[1].name != "addy"
            or root.parents[2].name != "native"):
        raise ValueError("Expected an immutable Addy source revision")
    pinned = open_pinned("addy", root.parents[3], revision)
    if not os.path.samefile(pinned.plugin_root, root) or pinned.source.repository != "addyosmani/agent-skills":
        raise ValueError("Invalid Addy repository root")
    return pinned


def provision(profile_home, upstream_plugin_root, revision):
    """Return live Codex discovery and file-verification receipts for a pinned Addy profile."""
    root = Path(upstream_plugin_root).resolve(strict=True)
    home = Path(profile_home).resolve()
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected an immutable Addy source revision")
    if home.name != revision or home.parent.name != "codex" or home.parents[1].name != "addy":
        raise ValueError("Expected a dedicated versioned Addy Codex home")
    if home.is_relative_to(root) or root.is_relative_to(home):
        raise ValueError("The private Codex home and upstream source must be separate")
    pinned = _source(root, revision)
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_bytes())
    if manifest["name"] != "agent-skills" or manifest["skills"] != "./skills/":
        raise ValueError("Unknown upstream Codex plugin layout")
    home.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / "factory-install.sqlite3", timeout=120)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return _install(home, root, pinned, manifest, revision)


def _install(home, root, pinned, manifest, revision):
    config_file = home / "config.toml"
    if config_file.exists():
        configured = tomllib.loads(config_file.read_text(encoding="utf-8"))
        if any(name != PLUGIN and settings.get("enabled") is True
               for name, settings in configured.get("plugins", {}).items()):
            raise RuntimeUnavailable("Another plugin is enabled in the Addy Codex profile")
    probe = home / "factory-probe"
    probe.mkdir(parents=True, exist_ok=True)
    with CodexRuntime(home, probe, read_only=True) as runtime:
        before = _catalog(runtime, pinned, revision)
        before["skills"] = sum(s.get("enabled") and s.get("pluginId") == PLUGIN for s in runtime.skills())
    if not (before["installed"] and before["enabled"]):
        added = _command(runtime, ["marketplace", "add", str(root)])
        if added["marketplaceName"] != "agent-skills" or Path(added["installedRoot"]).resolve() != root:
            raise RuntimeUnavailable("Codex registered an unexpected marketplace")
        installed = _command(runtime, ["add", PLUGIN])
        if installed["pluginId"] != PLUGIN or installed["version"] != manifest["version"]:
            raise RuntimeUnavailable("Codex installed an unexpected plugin revision")
    cache = home / "plugins" / "cache" / "agent-skills" / "agent-skills" / manifest["version"]
    plugin = {"plugin_id":PLUGIN, "installed_path":str(cache), "skills":sorted(pinned.skills)}
    with CodexRuntime(home, probe, read_only=True, native_plugin=plugin) as runtime:
        current = _catalog(runtime, pinned, revision)
        if not (current["installed"] and current["enabled"]):
            raise RuntimeUnavailable("Codex did not enable the native Addy plugin")
        enabled = runtime.isolate_skills()
        if any(s.get("pluginId") not in (None, PLUGIN) for s in enabled):
            raise RuntimeUnavailable("Another plugin is active in the Addy Codex profile")
        # Compare the exact host-reported entrypoint. The complete tree check
        # below opens every file with long-path support and rejects links.
        discovered = {s["name"]: Path(s["path"]) for s in enabled if s.get("pluginId") == PLUGIN}
        expected = {"agent-skills:" + name: cache / path.relative_to(root) for name, path in pinned.skills.items()}
        if not expected or discovered != expected:
            raise RuntimeUnavailable("Codex skill discovery differs from the pinned Addy manifest")
        verified_files = verify_same_files(root, cache)
        return {"revision": revision, "version": manifest["version"], "plugin_id": PLUGIN,
                "host": "codex", "host_version":runtime.version, "profile_home": str(home),
                "installed_path": str(cache), "source":str(root), "verified_files": verified_files,
                "skills":sorted(pinned.skills), "native_skill_names":sorted(discovered),
                "before":before, "after":{**current, "skills":len(discovered)},
                "author_agents_registered":False, "author_hooks_registered":False,
                "model_turns":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(json.dumps(provision(args.profile_home, args.upstream, args.revision), ensure_ascii=True))
