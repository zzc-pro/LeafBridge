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


def status(api, root, project_id, base):
    """Compare selected local files and the remote ZIP without changing files or baseline."""
    try:
        if not isinstance(base, dict):
            raise OverleafError("Invalid synchronization baseline. Check .leafbridge/state.json.")
        for name, digest in base.items():
            if (_path(name).as_posix() != name or name.endswith("/")
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise OverleafError("Invalid synchronization baseline. Check .leafbridge/state.json.")
        selected, tracked = git.files(root)
        # Metadata is never a source file, even if it was accidentally tracked.
        selected = {name for name in selected if PurePosixPath(name).parts[0].casefold() not in {".git", ".leafbridge"}}
        local = {}
        for name in sorted(selected):
            relative = _path(name)
            path = root.joinpath(*relative.parts)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise OverleafError(f"Local source path is a link or escapes the repository: {name!r}")
            if not path.exists():
                continue
            if not path.is_file():
                raise OverleafError(f"Local source path is not a regular file: {name!r}")
            with path.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                local[name] = digest.hexdigest()
        remote = _unpack(api.download(project_id))
        names = selected | remote.keys() | base.keys()
        names -= git.ignored(root, names - tracked)
        return {name: compare(local.get(name), remote.get(name), base.get(name)) for name in sorted(names)}
    except OverleafError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error):
        raise OverleafError("Unable to compare project files. Check local permissions and the downloaded ZIP.") from None


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
