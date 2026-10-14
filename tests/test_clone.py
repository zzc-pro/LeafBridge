"""Offline clone tests using mock downloads and temporary local repositories."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import warnings
import zipfile

from leafbridge import cli, client, git, sync
from test_login import BrowserError, session


def archive(files):
    """Build a source ZIP from test file names and bytes."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as output:
        for name, content in files.items() if isinstance(files, dict) else files:
            output.writestr(name, content)
    return data.getvalue()


@unittest.skipUnless(shutil.which("git"), "System Git is required for clone tests.")
class CloneTests(unittest.TestCase):
    def setUp(self):
        """Use a fake remote project and an isolated temporary destination."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "paper"
        self.api = MagicMock()
        self.api.projects.return_value = [{"id": "project-id", "name": "My Paper"}]
        self.files = {"main.tex": b"source", "figures/image.bin": b"\x00\xff", ".gitignore": b"*.aux"}
        self.api.download.return_value = archive(self.files)

    def test_files(self):
        """Download source files, initialize Git, and record the remote baseline."""
        self.assertEqual(sync.clone(self.api, "My Paper", self.target), self.target)
        self.api.download.assert_called_once_with("project-id")
        for name in ["main.tex", "figures/image.bin"]:
            self.assertEqual((self.target / name).read_bytes(), self.files[name])
        self.assertEqual((self.target / ".gitignore").read_bytes(), b"*.aux\n/.leafbridge/\n")
        metadata = self.target / ".leafbridge"
        config = json.loads((metadata / "config.json").read_text(encoding="utf-8"))
        state = json.loads((metadata / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(config["project_id"], "project-id")
        self.assertEqual(state["files"], {name: hashlib.sha256(content).hexdigest()
                                        for name, content in self.files.items()})
        self.assertTrue((self.target / ".git").is_dir())
        result = subprocess.run([git.check(), "ls-files", "--others", "--exclude-standard"],
                                cwd=self.target, capture_output=True, text=True, check=True, timeout=10)
        self.assertNotIn(".leafbridge/", result.stdout)
        head = subprocess.run([git.check(), "rev-parse", "--verify", "HEAD"],
                              cwd=self.target, capture_output=True, timeout=10)
        self.assertNotEqual(head.returncode, 0)

    def test_default(self):
        """Use the exact project name as the default destination directory."""
        with patch.object(sync.os, "getcwd", return_value=str(self.root)):
            target = sync.clone(self.api, "My Paper")
        self.assertEqual(target, self.root / "My Paper")

    def test_existing(self):
        """Preserve existing directories and avoid querying or downloading files."""
        self.target.mkdir()
        original = self.target / "keep.txt"
        original.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(client.OverleafError, "already exists"):
            sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(original.read_text(encoding="utf-8"), "keep")
        self.api.projects.assert_not_called()

    def test_missing(self):
        """Reject project names that do not exactly match an active project."""
        with self.assertRaisesRegex(client.OverleafError, "No active project"):
            sync.clone(self.api, "my paper", self.target)
        self.api.download.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_duplicate(self):
        """Reject ambiguous remote names instead of downloading the wrong project."""
        self.api.projects.return_value.append({"id": "other-id", "name": "My Paper"})
        with self.assertRaisesRegex(client.OverleafError, "Multiple active projects"):
            sync.clone(self.api, "My Paper", self.target)
        self.api.download.assert_not_called()

    def test_name(self):
        """Require an explicit local directory for names containing path separators."""
        with self.assertRaisesRegex(client.OverleafError, "Specify a local directory"):
            sync.clone(self.api, "Folder/Paper")
        self.api.projects.return_value = [{"id": "project-id", "name": "Folder/Paper"}]
        self.assertEqual(sync.clone(self.api, "Folder/Paper", self.target), self.target)

    def test_paths(self):
        """Reject traversal, absolute paths, and reserved metadata without leaving files."""
        for name in ["../outside.tex", "/outside.tex", "C:/outside.tex", "..\\outside.tex",
                     ".git/config", ".leafbridge/config.json"]:
            with self.subTest(name=name):
                self.api.download.return_value = archive({name: b"unsafe"})
                with self.assertRaises(client.OverleafError):
                    sync.clone(self.api, "My Paper", self.target)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_links(self):
        """Reject ZIP symlinks rather than following targets outside the clone."""
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.api.download.return_value = archive([(info, "../outside.tex")])
        with self.assertRaisesRegex(client.OverleafError, "unsafe"):
            sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_repeated_file(self):
        """Reject duplicate ZIP file paths without overwriting earlier content."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            self.api.download.return_value = archive([("main.tex", "first"), ("main.tex", "second")])
        with self.assertRaisesRegex(client.OverleafError, "duplicate"):
            sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_zip(self):
        """Reject invalid download contents and remove temporary extraction files."""
        self.api.download.return_value = b"<html>Login page</html>"
        with self.assertRaisesRegex(client.OverleafError, "downloaded ZIP"):
            sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_git_failure(self):
        """Leave no clone directory when Git initialization fails."""
        with patch.object(sync.git, "init", side_effect=client.OverleafError("Git failed")):
            with self.assertRaisesRegex(client.OverleafError, "Git failed"):
                sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_publish_failure(self):
        """Remove the newly created destination if moving staged files fails."""
        with patch.object(sync.Path, "rename", side_effect=OSError("denied")):
            with self.assertRaises(client.OverleafError):
                sync.clone(self.api, "My Paper", self.target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli(self):
        """Pass the quoted project name and optional destination to the clone workflow."""
        for arguments, directory in [(["clone", "My Paper"], None),
                                     (["clone", "My Paper", "paper"], "paper")]:
            with patch.object(cli, "clone", return_value=self.target) as operation, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(operation.call_args.args[1:], ("My Paper", directory))
            self.assertIn("Project cloned to:", output.getvalue())

    def test_cli_failure(self):
        """Report clone errors without printing a success message."""
        with patch.object(cli, "clone", side_effect=client.OverleafError("Test failure")), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["clone", "My Paper"]), 1)
        self.assertNotIn("Project cloned to:", output.getvalue())
        self.assertIn("Clone failed:", errors.getvalue())


class DownloadTests(unittest.TestCase):
    def setUp(self):
        """Mock authenticated HTTP downloads without reading real credentials."""
        self.api = client.OverleafClient()
        loader = patch.object(self.api, "load", return_value=session())
        loader.start()
        self.addCleanup(loader.stop)
        self.request, self.playwright = MagicMock(), MagicMock()
        self.playwright.request.new_context.return_value = self.request
        manager = MagicMock()
        manager.__enter__.return_value = self.playwright
        backend = patch.object(client, "_playwright", return_value=(lambda: manager, BrowserError))
        backend.start()
        self.addCleanup(backend.stop)

    def test_zip(self):
        """Reuse saved cookies and return the ZIP bytes from the download endpoint."""
        data = archive({"main.tex": "source"})
        self.request.get.return_value.status = 200
        self.request.get.return_value.body.return_value = data
        self.assertEqual(self.api.download("project-id"), data)
        self.request.get.assert_called_once_with("/Project/project-id/download/zip", max_redirects=0)
        options = self.playwright.request.new_context.call_args.kwargs
        self.assertEqual(options["storage_state"]["cookies"], session()["cookies"])
        self.request.dispose.assert_called_once()

    def test_errors(self):
        """Reject login redirects, access denial, rate limits, and failed downloads."""
        for status in [302, 401, 403, 429, 500]:
            with self.subTest(status=status):
                self.request.get.return_value.status = status
                with self.assertRaises(client.OverleafError):
                    self.api.download("project-id")
        self.request.get.return_value.body.assert_not_called()

    def test_network(self):
        """Hide request credentials when a network download fails."""
        self.request.get.side_effect = BrowserError("private-cookie")
        with self.assertRaisesRegex(client.OverleafError, "Check your connection") as error:
            self.api.download("project-id")
        self.assertNotIn("private-cookie", str(error.exception))
        self.request.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
