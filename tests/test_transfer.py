"""Offline push, pull, forced overwrite, and per-file failure tests."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from leafbridge import cli, client, git, sync
from leafbridge.client import BASE_URL, OverleafError
from test_clone import archive
from test_login import BrowserError, session
from test_sync import digest


@unittest.skipUnless(shutil.which("git"), "System Git is required.")
class TransferTests(unittest.TestCase):
    def setUp(self):
        """Create an associated working tree and an in-memory remote project."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git.init(self.root)
        self.metadata = self.root / ".leafbridge"
        self.metadata.mkdir()
        (self.metadata / "config.json").write_text(json.dumps({"base_url": BASE_URL,
                      "project_id": "project-id", "name": "My Paper"}), encoding="utf-8")
        self.base = {}
        self.save()
        self.remote = {".gitignore": (self.root / ".gitignore").read_bytes()}
        self.api = MagicMock()
        self.api.download.side_effect = lambda project: archive(self.remote)
        self.api.upload.side_effect = self.upload

    def upload(self, project, name, content):
        """Simulate a confirmed upload without touching any real account."""
        self.remote[name] = content

    def save(self):
        """Write an isolated baseline for the next operation."""
        (self.metadata / "state.json").write_text(json.dumps({"version": 1, "files": self.base}), encoding="utf-8")

    def state(self):
        """Read the persisted per-file baseline after a synchronization attempt."""
        return json.loads((self.metadata / "state.json").read_text(encoding="utf-8"))["files"]

    def write(self, name, content):
        """Create a local source file and any required folders."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def go(self, direction, force=False):
        """Run the synchronization engine using the latest persisted baseline."""
        return sync.run(self.api, self.root, "project-id", self.state(), direction, force)

    def test_push(self):
        """Upload local changes and additions while preserving destination-side changes."""
        for name, content in {"a.tex": b"new", "sections/new.tex": b"local", "image.bin": b"\x00\xff",
                              "remote.tex": b"old"}.items():
            self.write(name, content)
        self.remote.update({"a.tex": b"old", "remote.tex": b"remote edit", "remote-only.tex": b"keep"})
        self.base = {"a.tex": digest(b"old"), "remote.tex": digest(b"old")}
        self.save()
        count, skipped = self.go("push")
        self.assertEqual(count, 3)
        self.assertEqual(skipped, {"remote-only.tex": "remote only", "remote.tex": "remote modified"})
        self.assertEqual(self.remote["a.tex"], b"new")
        self.assertEqual(self.remote["sections/new.tex"], b"local")
        self.assertEqual(self.remote["image.bin"], b"\x00\xff")
        self.assertEqual(self.remote["remote.tex"], b"remote edit")
        self.assertEqual(self.state()["a.tex"], digest(b"new"))
        self.assertEqual(self.state()["remote.tex"], digest(b"old"))
        self.assertEqual(self.go("push")[0], 0)
        self.assertEqual(self.api.upload.call_count, 3)
        head = subprocess.run([git.check(), "-C", str(self.root), "rev-parse", "--verify", "HEAD"],
                              capture_output=True, timeout=30)
        self.assertNotEqual(head.returncode, 0)

    def test_pull(self):
        """Download remote changes and additions while preserving local changes and metadata."""
        self.write("a.tex", b"old")
        self.write("local.tex", b"local edit")
        self.write("local-only.tex", b"keep")
        self.remote.update({"a.tex": b"remote edit", "local.tex": b"old", "sections/new.tex": b"source",
                            "figures/image.bin": b"\x00\xff"})
        self.base = {"a.tex": digest(b"old"), "local.tex": digest(b"old")}
        self.save()
        config = (self.metadata / "config.json").read_bytes()
        count, skipped = self.go("pull")
        self.assertEqual(count, 3)
        self.assertEqual(skipped, {"local-only.tex": "local only", "local.tex": "local modified"})
        self.assertEqual((self.root / "a.tex").read_bytes(), b"remote edit")
        self.assertEqual((self.root / "local.tex").read_bytes(), b"local edit")
        self.assertEqual((self.root / "figures/image.bin").read_bytes(), b"\x00\xff")
        self.assertEqual((self.metadata / "config.json").read_bytes(), config)
        self.assertEqual(self.state()["a.tex"], digest(b"remote edit"))
        self.assertEqual(self.go("pull")[0], 0)
        self.api.upload.assert_not_called()

    def test_conflicts(self):
        """Pause the entire ordinary push or pull before any transfer or baseline update."""
        self.write("a-new.tex", b"upload")
        self.write("z-conflict.tex", b"local edit")
        self.write("same.tex", b"same")
        self.remote.update({"z-conflict.tex": b"remote edit", "b-new.tex": b"download", "same.tex": b"same"})
        self.base = {"z-conflict.tex": digest(b"old")}
        self.save()
        before = (self.metadata / "state.json").read_bytes()
        for direction in ["push", "pull"]:
            with self.assertRaisesRegex(OverleafError, "Conflicts detected.*paused") as error:
                self.go(direction)
            self.assertIn("z-conflict.tex", str(error.exception))
            self.assertEqual((self.metadata / "state.json").read_bytes(), before)
            self.assertFalse((self.root / "b-new.tex").exists())
            self.assertNotIn("a-new.tex", self.remote)
        self.api.upload.assert_not_called()

    def test_first(self):
        """Require force for differing first-sync contents, then establish the chosen baseline."""
        self.write("main.tex", b"local")
        self.remote["main.tex"] = b"remote"
        with self.assertRaisesRegex(OverleafError, "Conflicts"):
            self.go("push")
        self.assertEqual(self.go("push", True)[0], 1)
        self.assertEqual(self.remote["main.tex"], b"local")
        self.assertEqual(self.state()["main.tex"], digest(b"local"))
        self.remote["main.tex"] = b"new remote"
        self.write("main.tex", b"new local")
        self.assertEqual(self.go("pull", True)[0], 1)
        self.assertEqual((self.root / "main.tex").read_bytes(), b"new remote")
        self.assertEqual(self.state()["main.tex"], digest(b"new remote"))

    def test_force(self):
        """Force overwrites differing destination content but never deletes destination-only files."""
        self.write("a.tex", b"old")
        self.write("local-only.tex", b"preserve")
        self.remote.update({"a.tex": b"remote edit", "remote-only.tex": b"preserve"})
        self.base = {"a.tex": digest(b"old")}
        self.save()
        self.assertEqual(self.go("push", True)[0], 2)
        self.assertEqual(self.remote["a.tex"], b"old")
        self.assertEqual(self.remote["remote-only.tex"], b"preserve")
        self.write("a.tex", b"local edit")
        self.write("extra-local.tex", b"preserve")
        count, skipped = self.go("pull", True)
        self.assertEqual(count, 2)
        self.assertEqual((self.root / "a.tex").read_bytes(), b"old")
        self.assertEqual((self.root / "extra-local.tex").read_bytes(), b"preserve")
        self.assertIn("extra-local.tex", skipped)

    def test_missing(self):
        """Preserve files absent from the source, including forced deletion-versus-edit conflicts."""
        self.remote["main.tex"] = b"remote edit"
        self.base = {"main.tex": digest(b"old"), "gone.tex": digest(b"gone")}
        self.save()
        count, skipped = self.go("push", True)
        self.assertEqual(count, 0)
        self.assertEqual(skipped["main.tex"], "conflict")
        self.assertEqual(self.remote["main.tex"], b"remote edit")
        self.assertEqual(self.state()["main.tex"], digest(b"old"))
        self.assertIn("gone.tex", self.state())

    def test_equal(self):
        """Adopt equal content without transfer and retain fingerprints for absent files."""
        self.write("same.tex", b"same")
        self.remote["same.tex"] = b"same"
        self.base = {"same.tex": digest(b"old"), "gone.tex": digest(b"gone")}
        self.save()
        count, skipped = self.go("push")
        self.assertEqual(count, 0)
        self.assertEqual(skipped, {"gone.tex": "missing both"})
        self.assertEqual(self.state()["same.tex"], digest(b"same"))
        self.assertEqual(self.state()["gone.tex"], digest(b"gone"))
        self.api.upload.assert_not_called()

    def test_upload(self):
        """Persist the first upload only when a later file upload fails."""
        self.write("a.tex", b"a")
        self.write("b.tex", b"b")
        self.api.upload.side_effect = [None, OverleafError("Upload denied")]
        with self.assertRaisesRegex(OverleafError, "after 1 completed") as error:
            self.go("push")
        self.assertIn('"b.tex"', str(error.exception))
        self.assertEqual(self.state(), {"a.tex": digest(b"a")})

    def test_write(self):
        """Preserve old destination bytes and state when a later atomic file replacement fails."""
        self.remote.update({"a.tex": b"a", "b.tex": b"new"})
        self.write("b.tex", b"old")
        self.base = {"b.tex": digest(b"old")}
        self.save()
        replace = sync.os.replace

        def fail(source, target):
            """Fail only publication of the second local source file."""
            if target == self.root / "b.tex":
                raise OSError("Denied")
            return replace(source, target)

        with patch.object(sync.os, "replace", side_effect=fail), self.assertRaisesRegex(OverleafError, "after 1 completed"):
            self.go("pull")
        self.assertEqual((self.root / "a.tex").read_bytes(), b"a")
        self.assertEqual((self.root / "b.tex").read_bytes(), b"old")
        self.assertEqual(self.state(), {"a.tex": digest(b"a"), "b.tex": digest(b"old")})
        self.assertFalse(list(self.root.glob(".leafbridge-file-*")))

    def test_state(self):
        """Report a transferred file with unsaved state and preserve the previous JSON atomically."""
        self.write("a.tex", b"a")
        before = (self.metadata / "state.json").read_bytes()
        with patch.object(sync.os, "replace", side_effect=OSError("Denied")), \
                self.assertRaisesRegex(OverleafError, "may already have transferred"):
            self.go("push")
        self.assertEqual(self.remote["a.tex"], b"a")
        self.assertEqual((self.metadata / "state.json").read_bytes(), before)
        self.assertFalse(list(self.metadata.glob(".state-*")))

    def test_race(self):
        """Refuse to overwrite or upload local edits made after the initial snapshot."""
        for direction in ["push", "pull"]:
            self.write("main.tex", b"initial")
            self.remote["main.tex"] = b"remote"

            def download(project):
                """Simulate an editor changing the local file while the ZIP downloads."""
                self.write("main.tex", b"edited during sync")
                return archive(self.remote)

            self.api.download.side_effect = download
            with self.assertRaisesRegex(OverleafError, "changed during synchronization"):
                self.go(direction, True)
            self.assertEqual((self.root / "main.tex").read_bytes(), b"edited during sync")
            self.assertEqual(self.state(), {})
        self.api.upload.assert_not_called()

    def test_shape(self):
        """Refuse file-folder replacement even with force because deletion is disabled."""
        self.write("sections", b"local file")
        self.remote["sections/main.tex"] = b"remote file"
        for direction in ["push", "pull"]:
            with self.assertRaisesRegex(OverleafError, "Conflicts"):
                self.go(direction)
            with self.assertRaisesRegex(OverleafError, "without deletion"):
                self.go(direction, True)
            self.assertEqual((self.root / "sections").read_bytes(), b"local file")
            self.assertEqual(self.state(), {})
        self.api.upload.assert_not_called()

    def test_paths(self):
        """Validate all pull targets before downloading even the first source file to disk."""
        self.remote.update({"a.tex": b"a", "z-link.tex": b"z"})
        with patch.object(Path, "is_symlink", lambda path: path.name == "z-link.tex"), \
                self.assertRaisesRegex(OverleafError, "link or escapes"):
            self.go("pull", True)
        self.assertFalse((self.root / "a.tex").exists())
        self.assertEqual(self.state(), {})

    def test_case(self):
        """Reject cross-side case aliases before creating files that Windows cannot distinguish."""
        self.write("main.tex", b"local")
        self.remote["MAIN.tex"] = b"remote"
        with patch.object(sync.os.path, "normcase", lambda path: path.lower()):
            for direction in ["push", "pull"]:
                with self.assertRaisesRegex(OverleafError, "differ only in case"):
                    self.go(direction, True)
        self.assertEqual(self.state(), {})
        self.api.upload.assert_not_called()

    def test_ignored(self):
        """Force excludes tracked and untracked ignored files in both synchronization directions."""
        self.write(".gitignore", b"*.aux\n/.leafbridge/\n")
        self.remote[".gitignore"] = (self.root / ".gitignore").read_bytes()
        self.write("tracked.aux", b"local")
        subprocess.run([git.check(), "-C", str(self.root), "add", "-f", "tracked.aux"],
                       check=True, capture_output=True, timeout=30)
        self.write("local.aux", b"ignore")
        self.remote.update({"remote.aux": b"ignore", "tracked.aux": b"remote"})
        self.assertEqual(self.go("pull", True)[0], 0)
        self.assertEqual((self.root / "tracked.aux").read_bytes(), b"local")
        self.assertFalse((self.root / "remote.aux").exists())
        self.write("tracked.aux", b"new")
        self.assertEqual(self.go("push", True)[0], 0)
        self.assertNotIn("local.aux", self.remote)
        self.assertEqual(self.remote["tracked.aux"], b"remote")
        self.api.upload.assert_not_called()

    def test_ignore(self):
        """Preserve local ignore rules and their old baseline without pulling remote ignore files."""
        self.remote[".gitignore"] = b"*.aux\n"
        self.base = {".gitignore": digest((self.root / ".gitignore").read_bytes())}
        self.save()
        before = (self.root / ".gitignore").read_bytes()
        self.assertEqual(self.go("pull")[0], 0)
        self.assertEqual((self.root / ".gitignore").read_bytes(), before)
        self.assertEqual(self.state()[".gitignore"], digest(before))

    def test_hidden(self):
        """Exclude hidden files and folders from status, conflicts, push, pull, and force."""
        hidden = [".env", ".notes/main.tex", "sections/.draft.tex", "figures/.cache/image.bin"]
        for name in hidden:
            self.write(name, b"local hidden")
            self.remote[name] = b"remote hidden"
            self.base[name] = digest(b"old hidden")
        self.save()
        subprocess.run([git.check(), "-C", str(self.root), "add", "-f", ".env"],
                       check=True, capture_output=True, timeout=30)
        self.remote[".remote-only.tex"] = b"remote hidden"
        self.write("normal.tex", b"same")
        self.remote["normal.tex"] = b"same"
        rows = cli.status(self.api, self.root)[2]
        self.assertEqual(rows, {"normal.tex": "same"})
        for direction in ["push", "pull"]:
            for force in [False, True]:
                self.assertEqual(self.go(direction, force), (0, {}))
        for name in hidden:
            self.assertEqual((self.root / name).read_bytes(), b"local hidden")
            self.assertEqual(self.remote[name], b"remote hidden")
            self.assertEqual(self.state()[name], digest(b"old hidden"))
        self.assertFalse((self.root / ".remote-only.tex").exists())
        self.api.upload.assert_not_called()


class UploadTests(unittest.TestCase):
    def setUp(self):
        """Mock HTTP requests and credentials without invoking an actual browser."""
        self.api = client.OverleafClient()
        loader = patch.object(self.api, "load", return_value=session())
        loader.start()
        self.addCleanup(loader.stop)
        root = patch.object(self.api, "_root", return_value="root-id")
        root.start()
        self.addCleanup(root.stop)
        self.request, self.playwright = MagicMock(), MagicMock()
        self.playwright.request.new_context.return_value = self.request
        manager = MagicMock()
        manager.__enter__.return_value = self.playwright
        backend = patch.object(client, "_playwright", return_value=(lambda: manager, BrowserError))
        backend.start()
        self.addCleanup(backend.stop)
        self.request.post.return_value.status = 200
        self.request.post.return_value.json.return_value = {"success": True}

    def test_upload(self):
        """Send nested binary content, saved cookies, CSRF, timeout, and no redirects."""
        data = b"\x00\xff"
        self.api.upload("project-id", "figures/image.bin", data)
        self.request.post.assert_called_once_with("/project/project-id/upload", params={"folder_id": "root-id"}, max_redirects=0,
            multipart={"name": "image.bin", "relativePath": "figures/image.bin",
                       "qqfile": {"name": "image.bin", "mimeType": "application/octet-stream", "buffer": data}})
        options = self.playwright.request.new_context.call_args.kwargs
        self.assertEqual(options["storage_state"]["cookies"], session()["cookies"])
        self.assertEqual(options["extra_http_headers"]["X-CSRF-Token"], "test-csrf")
        self.assertEqual(options["timeout"], 60000)
        self.request.dispose.assert_called_once()

    def test_routes(self):
        """Retry a legacy route only after a 404 from the current lowercase route."""
        missing, success = MagicMock(), MagicMock()
        missing.status = 404
        success.status = 200
        success.json.return_value = {"success": True}
        self.request.post.side_effect = [missing, success]
        self.api.upload("project-id", "main.tex", b"source")
        calls = self.request.post.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ["/project/project-id/upload", "/Project/project-id/upload"])
        self.assertEqual(calls[0].kwargs, calls[1].kwargs)
        self.assertEqual(calls[0].kwargs["params"], {"folder_id": "root-id"})

    def test_missing(self):
        """Report incompatible routes clearly when both return 404."""
        self.request.post.return_value.status = 404
        with self.assertRaisesRegex(OverleafError, "both supported upload routes"):
            self.api.upload("project-id", "main.tex", b"source")
        self.assertEqual(self.request.post.call_count, 2)
        self.request.post.return_value.json.assert_not_called()

    def test_errors(self):
        """Reject HTTP errors, redirects, HTML, and unconfirmed application-level uploads."""
        for status in [302, 401, 403, 422, 429, 500]:
            self.request.post.reset_mock()
            self.request.post.return_value.status = status
            with self.subTest(status=status), self.assertRaises(OverleafError):
                self.api.upload("id", "main.tex", b"source")
            self.request.post.assert_called_once()
        self.request.post.return_value.status = 200
        for payload in [[], {}, {"success": False}, {"success": 1}]:
            self.request.post.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(OverleafError):
                self.api.upload("id", "main.tex", b"source")
        self.request.post.return_value.json.side_effect = ValueError("HTML")
        with self.assertRaisesRegex(OverleafError, "upload JSON"):
            self.api.upload("id", "main.tex", b"source")

    def test_network(self):
        """Hide request credentials when a network upload fails."""
        self.request.post.side_effect = BrowserError("private-cookie")
        with self.assertRaisesRegex(OverleafError, "Check your connection") as error:
            self.api.upload("id", "main.tex", b"source")
        self.assertNotIn("private-cookie", str(error.exception))
        self.request.post.assert_called_once()
        self.request.dispose.assert_called_once()

    def test_paths(self):
        """Reject dangerous relative paths before loading credentials or sending requests."""
        for name in ["../outside.tex", "/outside.tex", "a/../b.tex", ".git/config", ".leafbridge/config.json", "a\\b.tex",
                     ".gitignore", ".env", "sections/.draft.tex", "figures/.cache/image.bin"]:
            with self.subTest(name=name), self.assertRaises(OverleafError):
                self.api.upload("id", name, b"source")
        self.api.load.assert_not_called()
        self.request.post.assert_not_called()


class CliTests(unittest.TestCase):
    def test_args(self):
        """Route all four commands to the correct direction and explicit force choice."""
        for direction in ["push", "pull"]:
            for force in [False, True]:
                args = [direction, "--force"] if force else [direction]
                with patch.object(cli, "transfer", return_value=0) as transfer:
                    self.assertEqual(cli.main(args), 0)
                self.assertEqual(transfer.call_args.args[1:], (direction, force))

    def test_failure(self):
        """Print conflict and request errors without reporting synchronization completion."""
        for direction in ["push", "pull"]:
            for error, code in [(OverleafError("Conflicts detected. Synchronization paused."), 1),
                                (OverleafError("Upload denied"), 1), (KeyboardInterrupt(), 130)]:
                with patch.object(cli, "transfer", side_effect=error), redirect_stdout(io.StringIO()) as output, \
                        redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(cli.main([direction]), code)
                self.assertNotIn("complete", output.getvalue())
                self.assertIn(direction.capitalize(), errors.getvalue())
