"""Install Matt Pocock's pinned upstream marketplace using Codex's own plugin CLI."""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tomllib
from contextlib import closing

from .native_skills import open_pinned, verify_same_files
from .runtime import RuntimeUnavailable, _cli, _environment


MARKETPLACE = "mattpocock"
PLUGIN = "mattpocock-skills"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"


def _command(home, args):
    result = subprocess.run(_cli("codex") + ["plugin", *args, "--json"], cwd=home,
                            env=_environment(home, "CODEX_HOME"), stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeUnavailable("Codex could not install the local Matt marketplace or plugin")
    return json.loads(result.stdout)


def _source(path, revision):
    bundle = Path(path).resolve(strict=True)
    if (bundle.name != "plugin" or bundle.parent.name != revision or bundle.parents[1].name != "matt"
            or bundle.parents[2].name != "native"):
        raise ValueError("Matt plugin source must be a provisioned revision")
    profile = open_pinned("matt", bundle.parents[3], revision)
    if (profile.plugin_root.resolve() != bundle or profile.plugin_name != PLUGIN
            or profile.source.repository != "mattpocock/skills" or profile.source.subtree):
        raise ValueError("Pinned Matt source or manifest does not match")
    return profile


def provision(profile_home, upstream_plugin_root, revision):
    """Return the native host contract after verifying every installed file."""
    if not isinstance(revision, str) or not re.fullmatch("[a-f0-9]{40}", revision):
        raise ValueError("Expected a full Matt revision")
    home = Path(profile_home).resolve()
    if home.name != revision or home.parent.name != "codex" or home.parents[1].name != "matt":
        raise ValueError("Expected a dedicated versioned Matt Codex home")
    source = _source(upstream_plugin_root, revision)
    home.mkdir(parents=True, exist_ok=True)
    # Admission and native worker startup may both inspect this revision.
    # Serialize the CLI's marketplace/config/cache changes in this home alone.
    with closing(sqlite3.connect(home / "factory-install.sqlite3", timeout=120)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        return _install(home, source, revision)


def _install(home, source, revision):
    config_path = home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    if any(plugin_id != PLUGIN_ID and settings.get("enabled") is True
           for plugin_id, settings in config.get("plugins", {}).items()):
        raise ValueError("Another plugin is enabled in the Matt profile")
    market = config.get("marketplaces", {}).get(MARKETPLACE)
    if market is None:
        added = _command(home, ["marketplace", "add", str(source.plugin_root)])
        if added.get("marketplaceName") != MARKETPLACE or not os.path.samefile(added["installedRoot"], source.plugin_root):
            raise RuntimeUnavailable("Codex registered an unexpected Matt marketplace")
    else:
        if market.get("source_type") != "local":
            raise ValueError("An unpinned Matt marketplace is configured in the profile")
        registered = Path(market["source"])
        if not os.path.samefile(registered, source.plugin_root):
            # A previously verified immutable mirror of this exact commit can
            # serve an existing home when the caller's source cache has moved.
            previous = _source(registered, revision)
            if previous.source != source.source:
                raise ValueError("Registered Matt marketplace source differs from the task pin")
    installed = home / "plugins" / "cache" / MARKETPLACE / PLUGIN / source.version
    enabled = config.get("plugins", {}).get(PLUGIN_ID, {}).get("enabled") is True
    if not enabled or not installed.is_dir():
        receipt = _command(home, ["add", PLUGIN_ID])
        if (receipt.get("pluginId") != PLUGIN_ID or receipt.get("version") != source.version
                or not os.path.samefile(installed, receipt["installedPath"])):
            raise RuntimeUnavailable("Codex installed an unexpected Matt plugin")
    file_count = verify_same_files(source.plugin_root, installed)
    skills = sorted(source.skills)
    return {"plugin_id": PLUGIN_ID, "installed_path": str(installed), "revision": revision,
            "source_tree": source.source.tree, "verified_files": file_count,
            "version": source.version, "skills": skills, "native_skill_names": [f"{PLUGIN}:{name}" for name in skills]}
