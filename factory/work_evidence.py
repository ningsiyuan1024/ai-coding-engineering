"""Capture local Git evidence, with file contents kept out of telemetry."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


_LIMIT = 2 * 1024 * 1024
_FILES = ("committed.patch", "staged.patch", "unstaged.patch")
_PRIVATE_NAME = re.compile(
    r"(^\.env($|\.)|^id_(rsa|dsa|ecdsa|ed25519)($|\.)|\.(pem|p12|pfx|key|jks|kdbx)$|"
    r"\.tf(state(\.backup)?|vars)$|^(secrets?|credentials)\.(ya?ml|json|toml)$|"
    r"^(\.npmrc|\.pypirc|\.netrc|_netrc|\.git-credentials|credentials(\.json)?|auth\.json|"
    r"kubeconfig|application_default_credentials\.json)$)", re.I)
_PRIVATE_TEXT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    rb"(?:gh[pousr]_|github_pat_|sk-(?:ant-|proj-)?|lin_api_|xox[baprs]-)[A-Za-z0-9_-]{12,}|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]+\.|"
    rb"://[^/\s:@]+:[^/\s@]+@|\b(?:Bearer|Basic)[ \t]+[A-Za-z0-9+/=._-]{8,}|"
    rb"(?i:[A-Za-z0-9_.-]*(?:password|passwd|api[_-]?key|secret|access[_-]?token|"
    rb"token|credential|private[_-]?key)[\"']?\s*[:=]\s*[\"']?[^\s,;}{]{3,})")


def _link(info):
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _safe_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
        raise ValueError("Unexpected Git pathname")
    path = root
    for part in relative.parts:
        path = path / part
        try:
            if _link(path.lstat()):
                raise ValueError("link or reparse point")
        except (FileNotFoundError, NotADirectoryError):
            break
    return root / relative


def _checked_read(root, name):
    path = _safe_path(root, name)
    info = path.lstat()
    if _link(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    if info.st_size > _LIMIT:
        raise ValueError("size limit")
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        stream = path.open("rb")
        try:
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not 0 < length < len(buffer):
                raise ValueError("cannot verify opened file")
            actual = buffer.value.removeprefix("\\\\?\\")
            if actual.startswith("UNC\\"):
                actual = "\\\\" + actual[4:]
            if Path(actual) != path:
                raise ValueError("opened file path changed")
            data = stream.read(_LIMIT + 1)
        finally:
            stream.close()
    else:
        directory = os.open(root, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
        try:
            parts = Path(name).parts
            for component in parts[:-1]:
                child = os.open(component, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("not a regular file")
                data = stream.read(_LIMIT + 1)
        finally:
            os.close(directory)
    if len(data) > _LIMIT:
        raise ValueError("size limit")
    return data, info.st_mode


class _Repo:
    def __init__(self, path):
        self.path = path
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="",
                        GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
        self.options = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
        filters = self.call("config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$",
                            accepted=(0, 1))
        for key in filters.decode("utf-8").splitlines():
            self.options.extend(["-c", key + "=", "-c", key.rsplit(".", 1)[0] + ".required=false"])
        self.gitdir = Path(os.fsdecode(self.call("rev-parse", "--absolute-git-dir").rstrip(b"\r\n")))

    def call(self, *args, stdin=None, snapshot=None, accepted=(0,)):
        env = self.env
        if snapshot is not None:
            env = {**env, "GIT_DIR": str(self.gitdir), "GIT_WORK_TREE": str(snapshot)}
        command = ["git", *self.options, "-C", str(snapshot or self.path), *args]
        result = subprocess.run(command, env=env, input=stdin, capture_output=True, timeout=120)
        if result.returncode not in accepted:
            raise RuntimeError(f"Git evidence operation {args[0]} failed (exit {result.returncode})")
        return result.stdout

    def revision(self, ref):
        return self.call("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip().decode("ascii")


def _names(data):
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def _untracked(repo, tracked):
    tracked_dirs = {str(parent.as_posix()) for name in tracked
                    for parent in Path(name).parents if parent != Path(".")}
    pending = [repo.path]
    names, links = [], []
    while pending:
        candidates = {}
        for directory in pending:
            if _link(directory.lstat()):
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.casefold() == ".git":
                        continue
                    path = Path(entry.path)
                    name = path.relative_to(repo.path).as_posix()
                    info = entry.stat(follow_symlinks=False)
                    is_link = _link(info)
                    is_dir = stat.S_ISDIR(info.st_mode) and not is_link
                    if is_link:
                        links.append(name)
                    if name not in tracked:
                        candidates[name + "/" if is_dir else name] = is_dir
        pending = []
        if candidates:
            items = b"".join(b"./" + os.fsencode(name) + b"\0" for name in candidates)
            ignored = {n.removeprefix("./") for n in _names(repo.call(
                "check-ignore", "--no-index", "-z", "--stdin", stdin=items, accepted=(0, 1)))}
            for name, is_dir in candidates.items():
                if name in ignored and not (is_dir and name.rstrip("/") in tracked_dirs):
                    continue
                if is_dir:
                    pending.append(repo.path / name)
                else:
                    names.append(name)
    return sorted(names), sorted(links)


def _changes(raw):
    fields = iter(_names(raw))
    for header in fields:
        before, after, old_oid, new_oid, operation = header[1:].split()
        first = next(fields)
        second = next(fields) if operation[0] in {"R", "C"} else first
        entry = {"status": operation, "path": second}
        if second != first:
            entry["old_path"] = first
        yield entry, (before, after), (old_oid, new_oid)


def _omit_reason(name, body):
    if _PRIVATE_TEXT.search(os.fsencode(name)):
        return "possible credential in filename"
    if any(_PRIVATE_NAME.search(part) or part.lower() in {".ssh", ".aws", ".gnupg", ".docker", ".kube"}
           for part in Path(name).parts):
        return "sensitive filename"
    if b"\0" in body:
        return "binary data"
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return "non-UTF-8 content"
    if _PRIVATE_TEXT.search(body):
        return "possible credential"
    return None


def _artifacts(directory):
    manifest = json.loads(_safe_path(directory, "worktree.json").read_bytes())
    results = []
    for name in (*_FILES, "worktree.json"):
        path = _safe_path(directory, name)
        digest = _digest(path)
        if name in _FILES and digest != manifest["patch_sha256"][name]:
            raise ValueError("Stored worktree patch does not match its receipt")
        results.append((path, "worktree-manifest" if name.endswith(".json") else "git-diff", digest))
    return results


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def capture(cwd, base, run_directory):
    """Return (private path, artifact kind, SHA-256) for one immutable local snapshot."""
    if _link(Path(cwd).lstat()) or _link(Path(run_directory).lstat()):
        raise ValueError("Evidence roots must be ordinary directories")
    cwd, run_directory = Path(cwd).resolve(strict=True), Path(run_directory).resolve(strict=True)
    if run_directory.is_relative_to(cwd):
        raise ValueError("Private run evidence must live outside the contribution worktree")
    dest = run_directory / "worktree-evidence"
    repo = _Repo(cwd)
    if Path(os.fsdecode(repo.call("rev-parse", "--show-toplevel").rstrip(b"\r\n"))).resolve() != cwd:
        raise ValueError("Expected the task worktree root")
    baseline, head = repo.revision(base), repo.revision("HEAD")
    if dest.exists():
        if _link(dest.lstat()):
            raise ValueError("Evidence destination is a link")
        prior = json.loads(_safe_path(dest, "worktree.json").read_bytes())
        if prior["base"] != baseline:
            raise ValueError("The run already has evidence for a different baseline")
        return _artifacts(dest)
    repo.call("merge-base", "--is-ancestor", baseline, head)
    index = repo.call("ls-files", "--stage", "-z")
    tracked = {entry.split("\t", 1)[1] for entry in _names(index)}
    untracked, links = _untracked(repo, tracked)
    def display(name):
        if _PRIVATE_TEXT.search(os.fsencode(name)):
            return "[redacted-" + hashlib.sha256(os.fsencode(name)).hexdigest()[:16] + "]"
        return name

    linked_tracked = [p for p in links if p in tracked or any(n.startswith(p + "/") for n in tracked)]
    manifest = {"base": baseline, "head": head, "index_sha256": hashlib.sha256(index).hexdigest(),
                "content_limit_bytes_per_file": _LIMIT,
                "untracked_paths": [display(p) for p in untracked],
                "untracked_contents": "not collected", "layers": {}, "patch_sha256": {}}
    differences = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all"]
    with tempfile.TemporaryDirectory(prefix=".evidence-", dir=run_directory) as temp:
        private = Path(temp) / "publish"
        private.mkdir(mode=0o700)
        snapshot = Path(temp) / "work"
        snapshot.mkdir(mode=0o700)
        for stage, arguments in (("committed", [baseline, head]), ("staged", ["--cached", head]),
                                 ("unstaged", [])):
            work = stage == "unstaged"
            inventory = (["diff-files", "--no-renames", "--ignore-submodules=all"] if work else
                         [*differences, "--find-renames", *arguments])
            # Omit reparse points from working-tree traversal. Record tracked ones explicitly.
            excluded = [f":(exclude,top,literal){p}" for p in linked_tracked] if work else []
            raw = repo.call(*inventory, "--raw", "--no-abbrev", "-z", "--", *excluded)
            records = manifest["layers"][stage] = []
            patch = private / (stage + ".patch")
            with patch.open("xb") as output:
                for entry, modes, oids in _changes(raw):
                    records.append(entry)
                    names = [entry["path"]]
                    if "old_path" in entry:
                        names.insert(0, entry["old_path"])
                    reason = next((why for n in names if (why := _omit_reason(n, b""))), None)
                    if any(mode in {"120000", "160000"} for mode in modes):
                        reason = "link or submodule"
                    if entry["status"].startswith("U"):
                        reason = "unmerged index"
                    for oid in set(oids):
                        if not reason and set(oid) != {"0"}:
                            if int(repo.call("cat-file", "-s", oid)) > _LIMIT:
                                reason = "size limit"
                            else:
                                reason = _omit_reason(entry["path"], repo.call("cat-file", "blob", oid))
                    if not reason and work and entry["status"] != "D":
                        try:
                            data, mode = _checked_read(cwd, entry["path"])
                            reason = _omit_reason(entry["path"], data)
                            if not reason:
                                copy = snapshot / entry["path"]
                                copy.parent.mkdir(parents=True, exist_ok=True)
                                copy.write_bytes(data)
                                copy.chmod(stat.S_IMODE(mode))
                        except ValueError as error:
                            reason = str(error)
                    if reason:
                        entry["content_omitted"] = reason
                        continue
                    delta = repo.call("--literal-pathspecs", *differences, "--full-index", "--find-renames",
                                      *arguments, "--", *names, snapshot=snapshot)
                    reason = _omit_reason(entry["path"], delta)
                    if reason:
                        entry["content_omitted"] = reason
                    elif delta:
                        output.write(delta)
                    elif work:
                        records.pop()  # Stat-only difference; Git found no content or mode change.
                if work:
                    records.extend({"path": p, "status": "uninspected", "content_omitted": "link or reparse point"}
                                   for p in linked_tracked)
            for entry in records:
                entry["path"] = display(entry["path"])
                if "old_path" in entry:
                    entry["old_path"] = display(entry["old_path"])
            manifest["patch_sha256"][patch.name] = _digest(patch)
        if head != repo.revision("HEAD") or index != repo.call("ls-files", "--stage", "-z"):
            raise RuntimeError("Task HEAD or index changed during evidence capture")
        (private / "worktree.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        for artifact in private.iterdir():
            artifact.chmod(0o600)
        try:
            private.rename(dest)
        except OSError:
            if not dest.is_dir() or _link(dest.lstat()):
                raise
            if (dest / "worktree.json").read_bytes() != (private / "worktree.json").read_bytes():
                raise RuntimeError("Concurrent evidence captures disagree")
    return _artifacts(dest)
