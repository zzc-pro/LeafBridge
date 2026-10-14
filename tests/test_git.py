"""Tests for local repository setup."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from leafbridge import git
from leafbridge.client import OverleafError


class GitTests(unittest.TestCase):
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
