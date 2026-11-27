"""Install the pinned upstream pstack bundle for an isolated Cursor profile."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .native_skills import Source, _git_hash, _github, _projection, _unpack, _verify_tree


SOURCE = Source(
    "cursor/plugins",
    "b7b93c36be9bcc36a74db438e0fd385d660bc559",
    "pstack",
    "971573532de6714a4ddff8ec39b52d021ebf161a",
)

_PLUGIN_PATH = Path("home/.cursor/plugins/local/pstack")
_MANIFEST_PATH = Path(".cursor-plugin/plugin.json")
_CURSOR_SYSTEM_ENV = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR", "OS",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "TEMP", "TMP", "TMPDIR",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
})


@dataclass(frozen=True)
class CursorProfile:
    revision: str
    home: Path
    plugin_root: Path
    plugin_name: str
    version: str
    skills: dict[str, Path]
    agents: dict[str, Path]
    source: Source


def _io_path(path):
    # The full bundled automation tree exceeds Windows MAX_PATH at normal
    # workspace depths. Keep extended paths for filesystem I/O only.
    if os.name != "nt":
        return path
    name = os.path.normpath(os.path.abspath(path))
    if name.startswith("\\\\?\\"):
        return path
    if name.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + name[2:])
    return Path("\\\\?\\" + name)


def _redirected(path):
    try:
        details = _io_path(path).lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_path(path):
    if _redirected(path):
        raise ValueError("Cursor profile may not redirect a component outside the isolated home")


def _inspect(directory):
    for path in (directory, directory / "git-index.json", *(directory / p for p in _PLUGIN_PATH.parents)):
        _check_path(path)
    display_plugin = directory / _PLUGIN_PATH
    _check_path(display_plugin)
    plugin = _io_path(display_plugin)
    entries = json.loads(_io_path(directory / "git-index.json").read_text(encoding="utf-8"))
    _verify_tree(entries, SOURCE)
    expected = _projection(entries)
    seen = set()
    for current, folders, files in os.walk(plugin, followlinks=False):
        for item in folders + files:
            _check_path(Path(current) / item)
        for item in files:
            path = Path(current) / item
            name = path.relative_to(plugin).as_posix()
            original = expected.get(name)
            if original is None or _git_hash("blob", path.read_bytes()) != entries[original][1]:
                raise ValueError(f"Unexpected or changed Cursor plugin file: {name}")
            if os.name != "nt" and bool(path.stat().st_mode & 0o111) != (entries[original][0] == "100755"):
                raise ValueError(f"Changed Cursor plugin executable mode: {name}")
            seen.add(name)
    if seen != expected.keys():
        raise ValueError("Pinned Cursor plugin is incomplete")

    manifest = json.loads((plugin / _MANIFEST_PATH).read_text(encoding="utf-8"))
    if manifest["name"] != "pstack" or manifest["skills"] != "./skills/" or manifest["agents"] != "./agents/":
        raise ValueError("Pinned Cursor manifest has unexpected entrypoints")
    skills = {p.parent.name: display_plugin / p.relative_to(plugin)
              for p in sorted((plugin / "skills").glob("*/SKILL.md"))}
    agents = {p.stem: display_plugin / p.relative_to(plugin) for p in sorted((plugin / "agents").glob("*.md"))}
    if not {"poteto-mode", "setup-pstack"} <= skills.keys() or "poteto-agent" not in agents:
        raise ValueError("Pinned Cursor skill or agent entrypoint is missing")
    return CursorProfile(SOURCE.revision, directory / "home", display_plugin, "pstack",
                         manifest["version"], skills, agents, SOURCE)


def provision(line_root, *, pinned=None, archive=None):
    """Stage and verify original Cursor manifest, skills, agents and supporting files."""
    if pinned not in (None, SOURCE.revision):
        raise ValueError("No reviewed Cursor pstack source for the requested revision")
    requested = Path(os.path.abspath(line_root))
    _check_path(requested)
    base = requested.resolve()
    user_home = Path.home().resolve()
    if base == user_home or base.is_relative_to(user_home / ".cursor"):
        raise ValueError("Cursor pstack requires a dedicated profile directory")
    root = base / "cursor"
    _check_path(root)
    _io_path(root).mkdir(parents=True, exist_ok=True)
    destination = root / SOURCE.revision
    _check_path(destination)
    if not _io_path(destination).exists():
        with tempfile.TemporaryDirectory(prefix="pstack-", dir=_io_path(root)) as temp:
            scratch = Path(temp)
            source_archive = Path(archive) if archive is not None else scratch / "source.tar.gz"
            if archive is None:
                with source_archive.open("wb") as output:
                    subprocess.run(
                        ["gh", "api", "--hostname", "github.com", f"repos/{SOURCE.repository}/tarball/{SOURCE.revision}"],
                        stdout=output, check=True, timeout=180,
                    )
            stage = scratch / "profile"
            stage.mkdir()
            _unpack(source_archive, stage, SOURCE)
            bundle = stage / _PLUGIN_PATH
            bundle.parent.mkdir(parents=True)
            (stage / "plugin").rename(bundle)
            _inspect(stage)
            try:
                stage.rename(_io_path(destination))
            except OSError:
                if not _io_path(destination).is_dir():
                    raise
    return _inspect(destination)


def cursor_environment(profile, base=None):
    """Build an isolated, unauthenticated environment for Cursor metadata probes."""
    original = os.environ if base is None else base
    env = {name: value for name, value in original.items() if name.upper() in _CURSOR_SYSTEM_ENV}
    env.update(
        HOME=str(profile.home),
        USERPROFILE=str(profile.home),
        CURSOR_CONFIG_DIR=str(profile.home / ".cursor"),
        XDG_CONFIG_HOME=str(profile.home / ".config"),
        APPDATA=str(profile.home / "AppData/Roaming"),
        LOCALAPPDATA=str(profile.home / "AppData/Local"),
    )
    return env


def check_upstream():
    """Read the official default branch's original Cursor plugin tree."""
    prefix = f"repos/{SOURCE.repository}"
    document = _github(f"{prefix}/commits/HEAD")
    revision = document["sha"]
    root_tree = document["commit"]["tree"]["sha"]
    if not all(re.fullmatch("[0-9a-f]{40}", item) for item in (revision, root_tree)):
        raise ValueError("Invalid upstream Cursor Git object")
    tree = _github(f"{prefix}/git/trees/{root_tree}")
    if tree.get("truncated"):
        raise ValueError("Incomplete upstream Cursor root tree")
    matches = [entry["sha"] for entry in tree["tree"]
               if entry["path"] == SOURCE.subtree and entry["type"] == "tree"]
    if len(matches) != 1 or not re.fullmatch("[0-9a-f]{40}", matches[0]):
        raise ValueError("Original Cursor pstack tree is missing")
    candidate = Source(SOURCE.repository, revision, SOURCE.subtree, matches[0])
    if candidate.revision == SOURCE.revision and candidate != SOURCE:
        raise ValueError("Upstream does not match the reviewed Cursor pin")
    return candidate


def sync(line_root):
    """Report update candidates while keeping the reviewed selection for all claims."""
    selected = Path(line_root).resolve() / "cursor" / SOURCE.revision
    if selected.exists():
        _inspect(selected)
    candidate = check_upstream()
    return {
        "status": "current" if candidate == SOURCE else "pending_validation",
        "host": "cursor",
        "revision": SOURCE.revision,
        "tree": SOURCE.tree,
        "candidate_revision": candidate.revision,
        "candidate_tree": candidate.tree,
        "candidate_subtree": candidate.subtree,
    }
