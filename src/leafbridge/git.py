"""Small helpers for local Git repositories."""

from pathlib import Path
import shutil
import subprocess

from .client import OverleafError


def check():
    """Find system Git and fail clearly when it is not installed."""
    executable = shutil.which("git")
    if not executable:
        raise OverleafError("Git is not installed or is not on PATH. Install Git and try again.")
    return executable


def root(path=None):
    """Find the working tree root from an existing repository or its subdirectory."""
    try:
        result = subprocess.run([check(), "-C", str(Path.cwd() if path is None else path),
                                 "rev-parse", "--show-toplevel"], check=True,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        return Path(result.stdout.strip()).resolve()
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise OverleafError("Run this command inside a local Git working tree.") from None


def ignore(path):
    """Ignore LeafBridge metadata while preserving existing rules and rejecting symlinks."""
    directory = Path(path).resolve()
    target = directory / ".gitignore"
    if target.is_symlink() or target.resolve().parent != directory:
        raise OverleafError("The repository .gitignore must not be a symbolic link or point outside the repository.")
    try:
        with target.open("ab+") as stream:
            stream.seek(0)
            content = stream.read()
            if b"/.leafbridge/" in content.splitlines():
                return
            if content and not content.endswith(b"\n"):
                stream.write(b"\n")
            stream.write(b"/.leafbridge/\n")
    except OSError:
        raise OverleafError("Unable to update .gitignore. Check repository permissions.") from None


def files(path):
    """List tracked and untracked candidate paths using Git, before final ignore filtering."""
    try:
        result = subprocess.run([check(), "-C", str(path), "ls-files", "--cached", "--others",
                                 "--exclude-standard", "-z"], check=True,
                                capture_output=True, timeout=30)
        return {name for name in result.stdout.decode("utf-8").split("\0") if name}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise OverleafError("Unable to list local Git files. Check Git and repository permissions.") from None


def ignored(path, names):
    """Ask Git which candidate paths are ignored, including paths absent locally."""
    if not names:
        return set()
    try:
        data = "".join(name + "\0" for name in sorted(names)).encode("utf-8")
        result = subprocess.run([check(), "-C", str(path), "check-ignore", "--no-index", "-z", "--stdin"],
                                input=data, capture_output=True, timeout=30)
        if result.returncode not in {0, 1}:
            raise OverleafError("Unable to check Git ignore rules.")
        return {name for name in result.stdout.decode("utf-8").split("\0") if name}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise OverleafError("Unable to check Git ignore rules. Check Git and repository permissions.") from None


def init(path):
    """Initialize a local repository and ignore LeafBridge metadata without committing."""
    path = Path(path)
    try:
        subprocess.run([check(), "init", "--quiet", str(path)], check=True,
                       capture_output=True, timeout=30)
        ignore(path)
    except (OSError, subprocess.SubprocessError):
        raise OverleafError("Unable to initialize the local Git repository. Check Git and directory permissions.") from None
