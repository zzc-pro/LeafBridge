"""Offline tests for associating existing Git working trees with Overleaf."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from leafbridge import cli, git
from leafbridge.client import BASE_URL, OverleafError


@unittest.skipUnless(shutil.which("git"), "System Git is required.")
class RemoteTests(unittest.TestCase):
    def setUp(self):
        """Create an isolated repository and fake active project list."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "paper"
        self.repo.mkdir()
        subprocess.run([git.check(), "init", "--quiet", str(self.repo)], check=True,
                       capture_output=True, timeout=30)
        (self.repo / "main.tex").write_bytes(b"local source")
        (self.repo / ".gitignore").write_bytes(b"*.aux")
        self.api = MagicMock()
        self.project = {"id": "project-id", "name": "My Paper"}
        self.api.projects.return_value = [self.project]

    def test_add(self):
        """Associate from a subdirectory while preserving local files and Git remotes."""
        folder = self.repo / "sections"
        folder.mkdir()
        subprocess.run([git.check(), "-C", str(self.repo), "remote", "add", "origin", "https://example.com/paper.git"],
                       check=True, capture_output=True, timeout=30)
        root, project, created = cli.add(self.api, "My Paper", path=folder)
        self.assertEqual((root, project, created), (self.repo, self.project, True))
        metadata = self.repo / ".leafbridge"
        self.assertEqual(json.loads((metadata / "config.json").read_text(encoding="utf-8")),
                         {"base_url": BASE_URL, "project_id": "project-id", "name": "My Paper"})
        self.assertEqual(json.loads((metadata / "state.json").read_text()), {"version": 1, "files": {}})
        self.assertEqual((self.repo / "main.tex").read_bytes(), b"local source")
        self.assertEqual((self.repo / ".gitignore").read_bytes(), b"*.aux\n/.leafbridge/\n")
        result = subprocess.run([git.check(), "-C", str(self.repo), "remote", "get-url", "origin"],
                                check=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.strip(), "https://example.com/paper.git")
        self.api.download.assert_not_called()
        self.api.login.assert_not_called()

    def test_repeat(self):
        """Preserve the existing baseline when adding the same project by ID."""
        cli.add(self.api, "My Paper", path=self.repo)
        state = self.repo / ".leafbridge" / "state.json"
        state.write_bytes(b'{"version":1,"files":{"main.tex":"saved"}}')
        before = state.read_bytes()
        self.assertFalse(cli.add(self.api, project_id="project-id", path=self.repo)[2])
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual((self.repo / ".gitignore").read_bytes().count(b"/.leafbridge/"), 1)

    def test_other(self):
        """Reject switching projects without changing existing metadata."""
        cli.add(self.api, "My Paper", path=self.repo)
        config = self.repo / ".leafbridge" / "config.json"
        before = config.read_bytes()
        self.api.projects.return_value = [{"id": "other-id", "name": "Other"}]
        with self.assertRaisesRegex(OverleafError, "another project"):
            cli.add(self.api, "Other", path=self.repo)
        self.assertEqual(config.read_bytes(), before)

    def test_lookup(self):
        """Reject missing, ambiguous, or unavailable projects without local changes."""
        for projects in [[], [self.project, {"id": "other-id", "name": "My Paper"}]]:
            self.api.projects.return_value = projects
            with self.assertRaises(OverleafError):
                cli.add(self.api, "My Paper", path=self.repo)
        self.api.projects.side_effect = OverleafError("Session expired")
        with self.assertRaisesRegex(OverleafError, "Session expired"):
            cli.add(self.api, "My Paper", path=self.repo)
        self.assertFalse((self.repo / ".leafbridge").exists())
        self.assertEqual((self.repo / ".gitignore").read_bytes(), b"*.aux")

    def test_root(self):
        """Reject non-repositories and bare repositories before contacting Overleaf."""
        bare = self.root / "bare"
        subprocess.run([git.check(), "init", "--bare", "--quiet", str(bare)], check=True,
                       capture_output=True, timeout=30)
        for path in [self.root, bare]:
            with self.assertRaisesRegex(OverleafError, "working tree"):
                cli.add(self.api, "My Paper", path=path)
        self.api.projects.assert_not_called()

    def test_invalid(self):
        """Preserve incomplete or invalid existing metadata."""
        metadata = self.repo / ".leafbridge"
        metadata.mkdir()
        with self.assertRaises(OverleafError):
            cli.add(self.api, "My Paper", path=self.repo)
        config = metadata / "config.json"
        config.write_bytes(b"invalid json")
        with self.assertRaises(OverleafError):
            cli.add(self.api, "My Paper", path=self.repo)
        self.assertEqual(config.read_bytes(), b"invalid json")
        self.api.projects.assert_not_called()

    def test_failure(self):
        """Clean staged metadata when publishing or updating ignore rules fails."""
        for target in ["ignore", "rename"]:
            mock = patch.object(git, "ignore", side_effect=OverleafError("Denied")) if target == "ignore" else \
                patch.object(Path, "rename", side_effect=OSError("Denied"))
            with mock, self.assertRaises(OverleafError):
                cli.add(self.api, "My Paper", path=self.repo)
            self.assertFalse((self.repo / ".leafbridge").exists())
            self.assertFalse(list(self.repo.glob(".leafbridge-add-*")))

    def test_links(self):
        """Reject metadata and ignore symlinks without changing their external targets."""
        outside = self.root / "outside"
        outside.mkdir()
        link = self.repo / ".leafbridge"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks are unavailable.")
        with self.assertRaisesRegex(OverleafError, "symbolic link"):
            cli.add(self.api, "My Paper", path=self.repo)
        link.unlink()
        ignore = self.repo / ".gitignore"
        ignore.unlink()
        external = outside / "ignore"
        external.write_bytes(b"keep")
        ignore.symlink_to(external)
        with self.assertRaisesRegex(OverleafError, "symbolic link"):
            cli.add(self.api, "My Paper", path=self.repo)
        self.assertEqual(external.read_bytes(), b"keep")
        self.assertEqual(list(outside.iterdir()), [external])

    def test_link(self):
        """Reject a metadata symlink before reading configuration or querying projects."""
        with patch.object(Path, "is_symlink", return_value=True), \
                patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(OverleafError, "symbolic link"):
                cli.add(self.api, "My Paper", path=self.repo)
            read.assert_not_called()
        self.api.projects.assert_not_called()


class CliTests(unittest.TestCase):
    def test_args(self):
        """Pass the project name or ID to the association workflow."""
        for arguments, values in [(["remote", "add", "My Paper"], ("My Paper", None)),
                                   (["remote", "add", "--id", "project-id"], (None, "project-id"))]:
            with patch.object(cli, "add", return_value=(Path("paper"), {"id": "project-id", "name": "My Paper"}, True)) as add, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(add.call_args.args[1:], values)
            self.assertIn("Project associated:", output.getvalue())

    def test_invalid(self):
        """Reject missing or mutually exclusive arguments before starting work."""
        for arguments in [["remote"], ["remote", "add"], ["remote", "add", "Paper", "--id", "id"]]:
            with patch.object(cli, "add") as add, redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                cli.main(arguments)
            self.assertEqual(error.exception.code, 2)
            add.assert_not_called()

    def test_failure(self):
        """Report failures and cancellation without displaying success."""
        for error, code in [(OverleafError("Denied"), 1), (KeyboardInterrupt(), 130)]:
            with patch.object(cli, "add", side_effect=error), redirect_stdout(io.StringIO()) as output, \
                    redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["remote", "add", "Paper"]), code)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("Remote add", errors.getvalue())
