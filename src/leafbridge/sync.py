"""Local project setup and file transfer workflows."""

import hashlib
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


def _unpack(data, root):
    """Extract regular ZIP files within the target directory and record remote hashes."""
    hashes, seen = {}, set()
    root = root.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            parts = PurePosixPath(entry.filename).parts
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (not parts or PurePosixPath(entry.filename).is_absolute() or "\\" in entry.filename
                    or any(part in {".", ".."} or ":" in part or "\x00" in part for part in parts)
                    or parts[0].casefold() in {".git", ".leafbridge"}
                    or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or (os.name == "nt" and any(PureWindowsPath(part).is_reserved()
                                                or part.endswith((".", " ")) for part in parts))):
                raise OverleafError("The project ZIP contains an unsafe or reserved path.")
            target = root.joinpath(*parts).resolve()
            if not target.is_relative_to(root) or target == root:
                raise OverleafError("The project ZIP contains a path outside the clone directory.")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            key = os.path.normcase(str(target))
            if key in seen:
                raise OverleafError("The project ZIP contains duplicate file paths.")
            seen.add(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(entry) as source, target.open("xb") as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    destination.write(chunk)
                    digest.update(chunk)
            hashes[PurePosixPath(*parts).as_posix()] = digest.hexdigest()
    return hashes


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
