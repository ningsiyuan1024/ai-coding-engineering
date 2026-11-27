"""Install complete upstream plugin sources pinned to a commit and a Git tree."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath


@dataclass(frozen=True)
class Source:
    repository: str
    revision: str
    subtree: str
    tree: str


SOURCES = {
    "matt": Source("mattpocock/skills", "6f7f7831a625f040a8e06a0e7954826b87ce775a",
                   "", "54981f8195880713ce8e965efeb8d520bf803133"),
    "addy": Source("addyosmani/agent-skills", "e997ac5b9a74eab332d5e9793bb38a38c8cfdd74",
                   "", "e3efe619ae879ab25d9ccb5eabb1089620fcb315"),
}


@dataclass(frozen=True)
class NativeProfile:
    line: str
    revision: str
    plugin_root: Path
    plugin_name: str
    version: str
    skills: dict[str, Path]
    source: Source


def _git_hash(kind: str, body: bytes) -> str:
    header = f"{kind} {len(body)}\0".encode("ascii")
    return hashlib.sha1(header + body).hexdigest()


def verify_same_files(source, installed):
    """Verify the native package manager copied the entire pinned file tree."""
    def fail(error):
        raise error

    def fingerprints(directory):
        root = Path(directory).absolute()
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            name = str(root)
            root = Path("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name)
        def reject_link(path):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Unexpected link in the native plugin tree")

        reject_link(root)
        if not root.is_dir():
            raise ValueError("Missing native plugin directory")
        files = {}
        for current, dirs, names in os.walk(root, onerror=fail):
            for name in dirs + names:
                reject_link(Path(current) / name)
            for name in names:
                path = Path(current) / name
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).digest()
        return files

    expected = fingerprints(source)
    if not expected or fingerprints(installed) != expected:
        raise ValueError("Installed native plugin differs from the verified upstream tree")
    return len(expected)


def _tree_hash(entries: dict) -> str:
    directories = {}
    for name, (mode, value) in entries.items():
        parts = PurePosixPath(name).parts
        if (not parts or name != "/".join(parts) or PurePosixPath(name).is_absolute()
                or any(p in (".", "..") or "\\" in p or ":" in p
                       or PureWindowsPath(p).is_reserved() for p in parts)):
            raise ValueError("Invalid Git entry path")
        if mode not in {"100644", "100755", "120000"}:
            raise ValueError("Unsupported Git mode")
        node = directories
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("Conflicting Git entry paths")
        if parts[-1] in node:
            raise ValueError("Conflicting Git entry paths")
        digest = _git_hash("blob", value.encode()) if mode == "120000" else value
        node[parts[-1]] = (mode, digest)

    def digest_tree(node):
        items = []
        for name, item in node.items():
            is_dir = isinstance(item, dict)
            mode, digest = ("40000", digest_tree(item)) if is_dir else item
            entry = f"{mode} {name}\0".encode() + bytes.fromhex(digest)
            items.append((name.encode() + (b"/" if is_dir else b""), entry))
        return _git_hash("tree", b"".join(entry for _, entry in sorted(items)))

    return digest_tree(directories)


def _projection(entries):
    # Materialize the two upstream internal aliases as copies. This also works
    # on Windows without symbolic-link privileges. Preserve their Git link
    # targets in the index so that the upstream tree identity is verifiable.
    files = {name: name for name, (mode, _) in entries.items() if mode != "120000"}
    projected = files.copy()
    for name, (mode, target) in entries.items():
        if mode != "120000":
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if resolved in files:
            projected[name] = resolved
            continue
        children = [child for child in files if child.startswith(resolved + "/")]
        if not children:
            raise ValueError(f"Unsupported link in pinned plugin: {name}")
        for child in children:
            projected[name + child[len(resolved):]] = child
    return projected


def _verify_tree(entries, source):
    if _tree_hash(entries) != source.tree:
        raise ValueError(f"Git tree mismatch for {source.repository}@{source.revision}")


def _unpack(archive: Path, stage: Path, source: Source):
    entries = {}
    bodies = {}
    size = 0
    prefix = PurePosixPath(source.subtree).parts
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts[1:]  # GitHub archive wrapper
            if parts[:len(prefix)] != prefix or len(parts) <= len(prefix) or member.isdir():
                continue
            name = "/".join(parts[len(prefix):])
            if name in entries:
                raise ValueError("Duplicate archive member")
            if member.issym():
                entries[name] = ("120000", member.linkname)
            elif member.isfile():
                size += member.size
                if size > 32 * 1024 * 1024:
                    raise ValueError("Plugin exceeds supported unpacked size")
                body = tar.extractfile(member).read()
                mode = "100755" if member.mode & 0o111 else "100644"
                entries[name] = (mode, _git_hash("blob", body))
                bodies[name] = body
            else:
                raise ValueError("Unsupported archive member")

    # No archive-controlled path is used on disk until the complete tree has
    # matched the trusted, pinned upstream Git object.
    _verify_tree(entries, source)
    root = stage / "plugin"
    for name, original in _projection(entries).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bodies[original])
        if entries[original][0] == "100755":
            path.chmod(0o755)
    (stage / "git-index.json").write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _inspect(line, directory, source):
    root = directory / "plugin"
    index = directory / "git-index.json"
    if any(_is_link(path) for path in (directory.parent.parent, directory.parent, directory, root, index)):
        raise ValueError("Unexpected link in native profile")
    entries = json.loads(index.read_text(encoding="utf-8"))
    _verify_tree(entries, source)
    expected = _projection(entries)
    seen = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if _is_link(path):
                raise ValueError(f"Unexpected link in installed plugin: {path}")
        for name in files:
            path = Path(current) / name
            key = path.relative_to(root).as_posix()
            original = expected.get(key)
            if original is None or _git_hash("blob", path.read_bytes()) != entries[original][1]:
                raise ValueError(f"Unexpected or changed plugin file: {key}")
            if os.name != "nt" and bool(path.stat().st_mode & 0o111) != (entries[original][0] == "100755"):
                raise ValueError(f"Changed executable mode: {key}")
            seen.add(key)
    if seen != expected.keys():
        raise ValueError("Installed plugin is incomplete")
    # Codex accepts Matt's published compatibility manifest. Addy publishes a Codex manifest.
    codex_manifest = root / ".codex-plugin/plugin.json"
    manifest_path = codex_manifest if codex_manifest.is_file() else root / ".claude-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    spec = manifest.get("skills", "skills")
    if isinstance(spec, list):
        paths = [root / folder / "SKILL.md" for folder in spec]
    else:
        paths = sorted((root / spec).glob("*/SKILL.md"))
    if not paths or any(not path.resolve().is_relative_to(root) or not path.is_file() for path in paths):
        raise ValueError("Native skill entrypoint is missing")
    skills = {path.parent.name: path for path in paths}
    return NativeProfile(line, source.revision, root, manifest["name"], manifest["version"], skills, source)


def _store(line, profile_root):
    if line not in SOURCES:
        raise KeyError(line)
    native = Path(profile_root).resolve() / "native"
    owner = native / "owner"
    if any(_is_link(path) for path in (native, owner, native / line, native / "versions.sqlite3")):
        raise ValueError("Unexpected link in native profile")
    native.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=native) as temp:
        temp = Path(temp)
        if not owner.exists():
            candidate = temp / "owner"
            candidate.mkdir()
            (candidate / line).touch()
            try:
                candidate.rename(owner)
            except OSError:
                if not owner.is_dir():
                    raise
        if not (owner / line).is_file():
            raise ValueError("Profile belongs to another production line")
    return native


@contextmanager
def _registry(native):
    with closing(sqlite3.connect(native / "versions.sqlite3", timeout=20)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS versions (revision TEXT PRIMARY KEY, repository TEXT, subtree TEXT, tree TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS selection (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT)")
        yield db


def _selected(native, line):
    with _registry(native) as db:
        row = db.execute("SELECT repository, versions.revision, subtree, tree FROM versions "
                         "JOIN selection ON selection.revision=versions.revision WHERE id=1").fetchone()
    return Source(*row) if row else SOURCES[line]


def _install(line, native, source, archive):
    if not all(re.fullmatch("[0-9a-f]{40}", value) for value in (source.revision, source.tree)):
        raise ValueError("Invalid Git object identifier")
    dest = native / line / source.revision
    with tempfile.TemporaryDirectory(dir=native) as temp:
        temp = Path(temp)
        if not dest.exists():
            if archive is None:
                archive = temp / "source.tar.gz"
                with archive.open("wb") as output:
                    subprocess.run(["gh", "api", f"repos/{source.repository}/tarball/{source.revision}"],
                                   check=True, stdout=output, timeout=180)
            stage = temp / "bundle"
            stage.mkdir()
            _unpack(archive, stage, source)
            dest.parent.mkdir(exist_ok=True)
            try:
                stage.rename(dest)
            except OSError:
                if not dest.is_dir():
                    raise
    profile = _inspect(line, dest, source)
    with _registry(native) as db:
        db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?)",
                   (source.revision, source.repository, source.subtree, source.tree))
        row = db.execute("SELECT repository, revision, subtree, tree FROM versions WHERE revision=?",
                         (source.revision,)).fetchone()
        if Source(*row) != source:
            raise ValueError("Recorded source differs for the same pinned revision")
    return profile


def provision(line: str, profile_root: Path, *, archive: Path | None = None) -> NativeProfile:
    """Start a new task on the selected revision, defaulting to the checked-in pin.

    The caller registers the local plugin_root with its native package manager.
    An optional official tarball permits offline installation. No host is started.
    """
    native = _store(line, profile_root)
    return _install(line, native, _selected(native, line), archive)


def open_pinned(line: str, profile_root: Path, revision: str, *, expected_tree: str | None = None) -> NativeProfile:
    """Reopen a previously installed task revision offline and verify all files."""
    native = _store(line, profile_root)
    with _registry(native) as db:
        row = db.execute("SELECT repository, revision, subtree, tree FROM versions WHERE revision=?",
                         (revision,)).fetchone()
    if row is None:
        raise ValueError(f"No installed {line} pin for {revision}")
    source = Source(*row)
    if not re.fullmatch("[0-9a-f]{40}", revision) or (expected_tree and expected_tree != source.tree):
        raise ValueError("Task pin does not match installed source")
    return _inspect(line, native / line / revision, source)


def _github(path):
    output = subprocess.check_output(["gh", "api", path], timeout=60)
    return json.loads(output)


def check_upstream(line: str) -> Source:
    """Resolve the current official default branch to one immutable commit and subtree."""
    base = SOURCES[line]
    api = f"repos/{base.repository}"
    commit = _github(f"{api}/commits/HEAD")
    tree = commit["commit"]["tree"]["sha"]
    for part in PurePosixPath(base.subtree).parts:
        children = _github(f"{api}/git/trees/{tree}")
        matches = [entry["sha"] for entry in children["tree"]
                   if entry["type"] == "tree" and entry["path"] == part]
        if len(matches) != 1:
            raise ValueError(f"Upstream plugin subtree is unavailable: {base.subtree}")
        tree = matches[0]
    return Source(base.repository, commit["sha"], base.subtree, tree)


def sync(line: str, profile_root: Path, *, archive: Path | None = None) -> NativeProfile:
    """Periodic update entrypoint. Only new tasks receive the verified selection."""
    native = _store(line, profile_root)
    previous = _selected(native, line)
    candidate = check_upstream(line)
    profile = _install(line, native, candidate, archive)
    with _registry(native) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT revision FROM selection WHERE id=1").fetchone()
        current = row[0] if row else SOURCES[line].revision
        if current not in (previous.revision, candidate.revision):
            raise RuntimeError("Another sync changed the selected revision; retry")
        db.execute("INSERT OR REPLACE INTO selection VALUES (1, ?)", (candidate.revision,))
    return profile
