"""Offline tests for Git file selection and three-way synchronization status."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from leafbridge import cli, git, sync
from leafbridge.client import BASE_URL, OverleafError
from test_clone import archive


def digest(content):
    """Hash test bytes using the same fingerprint format as synchronization state."""
    return hashlib.sha256(content).hexdigest()


class CompareTests(unittest.TestCase):
    def test_cases(self):
        """Cover equality, first sync, one-sided edits, conflicts, and missing files."""
        cases = [
            ("a", "a", "a", "same"), ("b", "b", "a", "same"), ("b", "b", None, "same"),
            ("b", "a", "a", "local modified"), ("a", "b", "a", "remote modified"),
            ("b", "c", "a", "conflict"), ("b", "c", None, "conflict"),
            ("b", None, None, "local only"), (None, "b", None, "remote only"),
            (None, "a", "a", "missing local"), ("a", None, "a", "missing remote"),
            (None, "b", "a", "conflict"), ("b", None, "a", "conflict"),
            (None, None, "a", "missing both"),
        ]
        for local, remote, base, expected in cases:
            with self.subTest(local=local, remote=remote, base=base):
                self.assertEqual(sync.compare(local, remote, base), expected)


@unittest.skipUnless(shutil.which("git"), "System Git is required.")
class StatusTests(unittest.TestCase):
    def setUp(self):
        """Create an associated working tree with mocked remote ZIP contents."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git.init(self.root)
        self.metadata = self.root / ".leafbridge"
        self.metadata.mkdir()
        self.config = {"base_url": BASE_URL, "project_id": "project-id", "name": "My Paper"}
        self.state = {"version": 1, "files": {}}
        self.api = MagicMock()
        self.save()

    def save(self):
        """Write isolated test metadata without using real credentials."""
        for filename, data in [("config.json", self.config), ("state.json", self.state)]:
            (self.metadata / filename).write_text(json.dumps(data), encoding="utf-8")

    def write(self, name, content):
        """Create a test source file and any parent directories."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_status(self):
        """Inspect nested text and binary files without writing repository content or state."""
        local = {"main.tex": b"same", "sections/local.tex": b"local", "conflict.tex": b"blue",
                 "figures/image.bin": b"\x00\xff", "local.tex": b"changed", "remote.tex": b"original",
                 "missing-remote.tex": b"original"}
        remote = {"main.tex": b"same", "sections/remote.tex": b"remote", "conflict.tex": b"red",
                  "figures/image.bin": b"\x00\xff", "local.tex": b"original", "remote.tex": b"changed",
                  "missing-local.tex": b"original"}
        for name, content in local.items():
            self.write(name, content)
        self.state["files"] = {name: digest(b"original") for name in
                               ["local.tex", "remote.tex", "missing-local.tex", "missing-remote.tex", "missing-both.tex"]}
        self.save()
        self.api.download.return_value = archive(remote)
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        (self.root / "sections").mkdir(exist_ok=True)
        root, config, rows = cli.status(self.api, self.root / "sections")
        self.assertEqual(root, self.root)
        self.assertEqual(config, self.config)
        expected = {"main.tex": "same", "figures/image.bin": "same", "conflict.tex": "conflict",
                    "sections/local.tex": "local only", "sections/remote.tex": "remote only",
                    "local.tex": "local modified", "remote.tex": "remote modified",
                    "missing-local.tex": "missing local", "missing-remote.tex": "missing remote",
                    "missing-both.tex": "missing both"}
        self.assertEqual(rows, expected)
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes()
                                  for path in self.root.rglob("*") if path.is_file()})
        self.api.download.assert_called_once_with("project-id")
        self.api.login.assert_not_called()

    def test_selection(self):
        """Exclude tracked and untracked ignored files while honoring nested exceptions and Unicode."""
        self.write(".gitignore", b"*.aux\n/.leafbridge/\n")
        self.write("tracked.aux", b"tracked")
        subprocess.run([git.check(), "-C", str(self.root), "add", "-f", "tracked.aux"],
                       check=True, capture_output=True, timeout=30)
        self.write("local.aux", b"ignored")
        self.write("sections/.gitignore", b"*.tex\n!keep.tex\n")
        self.write("sections/hidden.tex", b"ignored")
        self.write("sections/keep.tex", b"kept")
        self.write("a space 中文.tex", b"unicode")
        self.api.download.return_value = archive({"tracked.aux": b"tracked", "remote.aux": b"ignored",
                                                  "sections/remote.tex": b"ignored", "sections/keep.tex": b"kept",
                                                  "a space 中文.tex": b"unicode"})
        rows = cli.status(self.api, self.root)[2]
        self.assertNotIn("tracked.aux", rows)
        self.assertEqual(rows["sections/keep.tex"], "same")
        self.assertEqual(rows["a space 中文.tex"], "same")
        for name in ["local.aux", "remote.aux", "sections/hidden.tex", "sections/remote.tex", ".leafbridge/config.json"]:
            self.assertNotIn(name, rows)

    def test_clone(self):
        """Compare cloned source files while excluding the local hidden ignore file."""
        target = self.root / "cloned"
        self.api.projects.return_value = [{"id": "project-id", "name": "My Paper"}]
        self.api.download.return_value = archive({"main.tex": b"source", ".gitignore": b"*.aux"})
        sync.clone(self.api, "My Paper", target)
        rows = cli.status(self.api, target)[2]
        self.assertEqual(rows, {"main.tex": "same"})

    def test_metadata(self):
        """Fail before network access when metadata is missing, malformed, or unsupported."""
        cases = [("config.json", b"bad json"), ("config.json", b"[]"),
                 ("config.json", b'{"base_url":"https://example.com","project_id":"id"}'),
                 ("state.json", b'{"version":true,"files":{}}'),
                 ("state.json", b'{"version":1,"files":{"main.tex":"bad hash"}}'),
                 ("state.json", json.dumps({"version": 1, "files": {"../outside": digest(b"bad")}}).encode())]
        for filename, content in cases:
            self.save()
            (self.metadata / filename).write_bytes(content)
            with self.subTest(filename=filename, content=content), self.assertRaises(OverleafError):
                cli.status(self.api, self.root)
        self.save()
        (self.metadata / "state.json").unlink()
        with self.assertRaisesRegex(OverleafError, "Missing"):
            cli.status(self.api, self.root)
        self.api.download.assert_not_called()

    def test_zip(self):
        """Reject corrupt ZIPs, traversal, and reserved remote metadata without writing files."""
        for data in [b"not a ZIP", archive({"../outside.tex": b"bad"}), archive({".git/config": b"bad"}),
                     archive({".leafbridge/state.json": b"bad"})]:
            self.api.download.return_value = data
            with self.assertRaises(OverleafError):
                cli.status(self.api, self.root)
        self.assertFalse((self.root / "outside.tex").exists())

    def test_failure(self):
        """Propagate a failed download instead of reporting a partial comparison."""
        self.api.download.side_effect = OverleafError("Session expired")
        with self.assertRaisesRegex(OverleafError, "Session expired"):
            cli.status(self.api, self.root)

    def test_link(self):
        """Reject metadata links without reading their destinations."""
        with patch.object(Path, "is_symlink", return_value=True), patch.object(Path, "read_text") as read:
            with self.assertRaisesRegex(OverleafError, "unsafe path"):
                cli.status(self.api, self.root)
            read.assert_not_called()
        self.api.download.assert_not_called()


class CliTests(unittest.TestCase):
    def test_output(self):
        """Print differences and counts while keeping control characters in paths escaped."""
        config = {"project_id": "project-id", "name": "My Paper"}
        rows = {"main.tex": "same", "local.tex": "local only", "conflict\n.tex": "conflict"}
        with patch.object(cli, "status", return_value=(Path("paper"), config, rows)), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("local only", output.getvalue())
        self.assertIn('"conflict\\n.tex"', output.getvalue())
        self.assertIn("same: 1; differences: 2; conflicts: 1", output.getvalue())

    def test_empty(self):
        """Report a completed inspection for matching or empty project contents."""
        for rows in [{}, {"main.tex": "same"}]:
            with patch.object(cli, "status", return_value=(Path("paper"), {"project_id": "id"}, rows)), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["status"]), 0)
            self.assertIn("No file differences found.", output.getvalue())

    def test_failure(self):
        """Report request failures or cancellation without printing success or partial rows."""
        for error, code in [(OverleafError("Download denied"), 1), (KeyboardInterrupt(), 130)]:
            with patch.object(cli, "status", side_effect=error), redirect_stdout(io.StringIO()) as output, \
                    redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["status"]), code)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("Status", errors.getvalue())
