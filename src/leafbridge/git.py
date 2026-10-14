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


def init(path):
    """Initialize a local repository and ignore LeafBridge metadata without committing."""
    path = Path(path)
    try:
        subprocess.run([check(), "init", "--quiet", str(path)], check=True,
                       capture_output=True, timeout=30)
        ignore = path / ".gitignore"
        with ignore.open("ab+") as stream:
            stream.seek(0)
            content = stream.read()
            if content and not content.endswith(b"\n"):
                stream.write(b"\n")
            stream.write(b"/.leafbridge/\n")
    except (OSError, subprocess.SubprocessError):
        raise OverleafError("Unable to initialize the local Git repository. Check Git and directory permissions.") from None
