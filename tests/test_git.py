"""Tests for local repository setup."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from leafbridge import git
from leafbridge.client import OverleafError


class GitTests(unittest.TestCase):
    def test_ignore(self):
        """Preserve ignore rules and avoid appending the metadata rule twice."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / ".gitignore"
            target.write_bytes(b"*.aux\r\n*.log")
            git.ignore(root)
            git.ignore(root)
            self.assertEqual(target.read_bytes(), b"*.aux\r\n*.log\n/.leafbridge/\n")

    def test_link(self):
        """Reject an ignore symlink before opening its destination."""
        with patch.object(Path, "is_symlink", return_value=True), \
                patch.object(Path, "open") as stream:
            with self.assertRaisesRegex(OverleafError, "symbolic link"):
                git.ignore(Path("unused"))
            stream.assert_not_called()

    def test_missing(self):
        """Report missing system Git before cloning any files."""
        with patch.object(git.shutil, "which", return_value=None):
            with self.assertRaisesRegex(OverleafError, "Git is not installed"):
                git.check()

    def test_failure(self):
        """Report Git initialization errors without exposing subprocess details."""
        with patch.object(git, "check", return_value="git"), \
                patch.object(git.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaisesRegex(OverleafError, "initialize"):
                git.init(Path("unused"))
