"""Local project setup and file transfer workflows."""

import hashlib
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import tempfile
import zipfile
import zlib

from . import git
from .client import BASE_URL, OverleafError


def _path(name):
    """Validate a relative source-file path before reading or extracting its contents."""
    if (not isinstance(name, str) or not name or "\\" in name
            or PurePosixPath(name).is_absolute()
            or any(part in {".", ".."} or ":" in part or "\x00" in part for part in name.rstrip("/").split("/"))
            or PurePosixPath(name).parts[0].casefold() in {".git", ".leafbridge"}
            or (os.name == "nt" and any(PureWindowsPath(part).is_reserved()
                                        or part.endswith((".", " ")) for part in PurePosixPath(name).parts))):
        raise OverleafError("A source file has an unsafe or reserved path.")
    return PurePosixPath(name)


def _hidden(name):
    """Exclude any path with a dot-prefixed file or directory component from synchronization."""
    return any(part.startswith(".") for part in PurePosixPath(name).parts)


def _unpack(data, root=None):
    """Hash safe ZIP entries and optionally extract them within a target directory."""
    hashes, seen = {}, set()
    root = root.resolve() if root is not None else None
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            parts = _path(entry.filename).parts
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise OverleafError("The project ZIP contains an unsafe or reserved path.")
            target = root.joinpath(*parts).resolve() if root is not None else None
            if target is not None and (not target.is_relative_to(root) or target == root):
                raise OverleafError("The project ZIP contains a path outside the clone directory.")
            if entry.is_dir():
                if target is not None:
                    target.mkdir(parents=True, exist_ok=True)
                continue
            key = os.path.normcase(PurePosixPath(*parts).as_posix())
            if key in seen:
                raise OverleafError("The project ZIP contains duplicate file paths.")
            seen.add(key)
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(entry) as source, (target.open("xb") if target is not None else nullcontext()) as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    if destination is not None:
                        destination.write(chunk)
                    digest.update(chunk)
            hashes[PurePosixPath(*parts).as_posix()] = digest.hexdigest()
    return hashes


def compare(local, remote, base):
    """Classify content differences against the last successful synchronization hash."""
    if local == remote:
        return "same" if local is not None else "missing both"
    if base is None:
        if local is None:
            return "remote only"
        if remote is None:
            return "local only"
        return "conflict"
    if local != base and remote != base:
        return "conflict"
    if local is None:
        return "missing local"
    if remote is None:
        return "missing remote"
    return "local modified" if local != base else "remote modified"


def _target(root, name):
    """Resolve a source path inside the working tree without following file or folder links."""
    path = root
    for part in _path(name).parts:
        path = path / part
        if path.is_symlink() or path.resolve() != path or not path.resolve().is_relative_to(root):
            raise OverleafError(f"Local source path is a link or escapes the repository: {name!r}")
    return path


def _hash(path):
    """Hash a regular local file, returning None when the file is absent."""
    if not path.exists():
        return None
    if not path.is_file():
        raise OverleafError(f"Local source path is not a regular file: {path.name!r}")
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan(api, root, project_id, base):
    """Read local and remote snapshots and classify all selected files before any writes."""
    try:
        if not isinstance(base, dict):
            raise OverleafError("Invalid synchronization baseline. Check .leafbridge/state.json.")
        for name, digest in base.items():
            if (_path(name).as_posix() != name or name.endswith("/")
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise OverleafError("Invalid synchronization baseline. Check .leafbridge/state.json.")
        selected = {name for name in git.files(root) if not _hidden(name)}
        # Git's no-index ignore check also excludes files that were already tracked.
        selected -= git.ignored(root, selected)
        local = {}
        for name in sorted(selected):
            digest = _hash(_target(root, name))
            if digest is not None:
                local[name] = digest
        data = api.download(project_id)
        remote = _unpack(data)
        names = {name for name in selected | remote.keys() | base.keys() if not _hidden(name)}
        names -= git.ignored(root, names)
        rows = {name: compare(local.get(name), remote.get(name), base.get(name)) for name in sorted(names)}
        present = (local.keys() | remote.keys()) & names
        seen = {}
        for name in present:
            key = os.path.normcase(name)
            if key in seen and seen[key] != name:
                raise OverleafError(f"Source paths differ only in case: {seen[key]!r} and {name!r}. Rename them before synchronization.")
            seen[key] = name
            for parent in PurePosixPath(name).parents:
                if parent.as_posix() in present:
                    rows[name] = rows[parent.as_posix()] = "conflict"
        return local, remote, rows, data
    except OverleafError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        raise OverleafError("Unable to compare project files. Check local permissions and the downloaded ZIP.") from None


def status(api, root, project_id, base):
    """Compare selected local files and the remote ZIP without changing files or baseline."""
    return _scan(api, root, project_id, base)[2]


def _save(root, files):
    """Atomically persist successful per-file fingerprints, preserving old state on failure."""
    metadata = root / ".leafbridge"
    target = metadata / "state.json"
    temporary = None
    try:
        if (metadata.is_symlink() or metadata.resolve() != metadata or target.is_symlink()
                or target.resolve().parent != metadata or not target.is_file()):
            raise OverleafError("Cannot save synchronization state through an unsafe or missing metadata path.")
        descriptor, name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=metadata)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "files": files}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        raise OverleafError("Unable to save synchronization state. A file may already have transferred or been deleted; run leaf status before retrying.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write(root, name, data, expected):
    """Replace one local file atomically after checking it still matches the inspected version."""
    target = _target(root, name)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix=".leafbridge-file-", dir=target.parent)
        temporary = Path(path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        target = _target(root, name)
        if _hash(target) != expected:
            raise OverleafError(f"Local file changed during synchronization: {name!r}. Run leaf status and retry.")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(api, root, project_id, base, direction, force=False, report=None):
    """Sync one direction; force mirrors selected source files, including deletions."""
    if direction not in {"push", "pull"}:
        raise OverleafError("Synchronization direction must be push or pull.")
    local, remote, rows, data = _scan(api, root, project_id, base)
    conflicts = [name for name, label in rows.items() if label == "conflict"]
    if conflicts and not force:
        paths = "\n".join("  " + json.dumps(name, ensure_ascii=False) for name in conflicts)
        raise OverleafError(f"Conflicts detected. Synchronization paused; no files or state were changed.\n{paths}\n"
                            f"Resolve conflicts or use leaf {direction} --force to prefer the source side.")
    source, destination = (local, remote) if direction == "push" else (remote, local)
    labels = {"local only", "local modified"} if direction == "push" else {"remote only", "remote modified"}
    changes = [name for name, label in rows.items() if name in source and source.get(name) != destination.get(name)
               and (force or label in labels)]
    deletions = [name for name in rows if name in destination and name not in source] if force else []
    forgotten = [name for name, label in rows.items() if label == "missing both"] if force else []
    skipped = {name: label for name, label in rows.items()
               if label != "same" and name not in changes and name not in deletions and name not in forgotten}
    # Validate every destination before touching any file, including structural conflicts.
    try:
        for name in deletions:
            target = _target(root, name)
            if _hash(target) != local.get(name):
                raise OverleafError(f"Local file changed during synchronization: {name!r}. Run leaf status and retry.")
        for name in changes:
            if direction == "pull":
                target = _target(root, name)
                for parent in target.parents:
                    if parent == root:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise OverleafError(f"Cannot replace a file with a folder without deletion: {name!r}")
                if target.exists() and not target.is_file():
                    raise OverleafError(f"Cannot replace a folder with a file without deletion: {name!r}")
            else:
                path = PurePosixPath(name)
                if (any(parent.as_posix() in remote for parent in path.parents)
                        or any(other.startswith(name + "/") for other in remote)):
                    raise OverleafError(f"Cannot replace a remote file or folder without deletion: {name!r}")
    except (OSError, RuntimeError) as error:
        if isinstance(error, OverleafError):
            raise
        raise OverleafError("Unable to validate synchronization paths. No files or state were changed.") from None
    saved = dict(base)
    transferred = 0
    name = None
    try:
        if report is not None:
            for name in deletions:
                report("Will delete remote" if direction == "push" else "Will delete local", name)
        with api.batch() if direction == "push" and (changes or deletions) else nullcontext():
            for name in changes:
                if direction == "push":
                    target = _target(root, name)
                    content = target.read_bytes()
                    if hashlib.sha256(content).hexdigest() != local[name]:
                        raise OverleafError(f"Local file changed during synchronization: {name!r}. Run leaf status and retry.")
                    api.upload(project_id, name, content)
                else:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        entry = next(entry for entry in archive.infolist() if not entry.is_dir()
                                     and PurePosixPath(entry.filename).as_posix() == name)
                        content = archive.read(entry)
                    _write(root, name, content, local.get(name))
                updated = {**saved, name: source[name]}
                _save(root, updated)
                saved = updated
                transferred += 1
                if report is not None:
                    report("Uploaded" if direction == "push" else "Downloaded", name)
            # Transfer first so an upload/download failure does not trigger planned deletions.
            for name in deletions:
                target = _target(root, name)
                if _hash(target) != local.get(name):
                    raise OverleafError(f"Local file changed during synchronization: {name!r}. Run leaf status and retry.")
                if direction == "push":
                    api.delete(project_id, name)
                else:
                    target.unlink()
                updated = {key: value for key, value in saved.items() if key != name}
                _save(root, updated)
                saved = updated
                transferred += 1
                if report is not None:
                    report("Deleted remote" if direction == "push" else "Deleted local", name)
        for name, label in rows.items():
            if label == "same" and saved.get(name) != local[name]:
                saved[name] = local[name]
                _save(root, saved)
        if forgotten:
            saved = {key: value for key, value in saved.items() if key not in forgotten}
            _save(root, saved)
    except (OverleafError, OSError, ValueError, zipfile.BadZipFile, RuntimeError, zlib.error) as error:
        reason = str(error) if isinstance(error, OverleafError) else "Check file permissions and downloaded contents."
        failed = f" File: {json.dumps(name, ensure_ascii=False)}." if name is not None else ""
        raise OverleafError(f"Synchronization stopped after {transferred} completed file operation(s).{failed} {reason}") from None
    return transferred, skipped


def clone(api, name, path=None):
    """Clone one active project by exact name into a new local Git repository."""
    if path is None and (not name or name in {".", ".."} or any(char in name for char in "/\\:")):
        raise OverleafError("The project name cannot be used as a directory. Specify a local directory after the name.")
    target = Path(name if path is None else path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise OverleafError(f"Destination already exists: {target}")
    target = target.resolve()
    if not target.parent.is_dir():
        raise OverleafError(f"Destination parent does not exist: {target.parent}")
    if os.name == "nt" and (target.is_reserved() or target.name.endswith((".", " "))):
        raise OverleafError("The destination directory name is not valid on Windows.")
    git.check()
    matches = [project for project in api.projects() if project["name"] == name]
    if not matches:
        raise OverleafError(f"No active project named {name!r}. Run leaf list to check the name.")
    if len(matches) != 1:
        raise OverleafError(f"Multiple active projects are named {name!r}. Rename one in Overleaf before cloning.")
    project = matches[0]
    try:
        data = api.download(project["id"])
        with tempfile.TemporaryDirectory(prefix=".leafbridge-clone-", dir=target.parent) as temporary:
            work = Path(temporary) / "project"
            work.mkdir()
            hashes = _unpack(data, work)
            git.init(work)
            metadata = work / ".leafbridge"
            metadata.mkdir()
            config = {"base_url": BASE_URL, "project_id": project["id"], "name": project["name"]}
            state = {"version": 1, "files": hashes}
            for filename, payload in [("config.json", config), ("state.json", state)]:
                (metadata / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
            target.mkdir()
            try:
                for entry in work.iterdir():
                    entry.rename(target / entry.name)
            except BaseException:
                if target.resolve() == target.absolute():
                    shutil.rmtree(target)
                raise
    except OverleafError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        raise OverleafError("Unable to clone the project. Check the downloaded ZIP and local directory permissions.") from None
    return target
