"""Offline regression tests: python -m unittest -v test_sync."""

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from leafbridge import sync
from leafbridge.client import OverleafError


def digest(content):
    return hashlib.sha256(content).hexdigest()


class FakeAPI:
    def __init__(self, files, on_batch=None):
        self.files = dict(files)
        self.uploads = []
        self.deletions = []
        self.on_batch = on_batch

    def projects(self):
        return [{"id": "paper", "name": "Paper"}]

    def download(self, project_id):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in self.files.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    @contextmanager
    def batch(self):
        if self.on_batch:
            self.on_batch()
        yield

    def upload(self, project_id, name, content):
        self.uploads.append((name, content))
        # Emulate Overleaf storing edited source text with LF line endings.
        self.files[name] = sync._lf_content(name, content)

    def delete(self, project_id, name):
        self.deletions.append(name)
        del self.files[name]


class SyncLineEndingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "repository"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True,
                       capture_output=True)
        metadata = self.root / ".leafbridge"
        metadata.mkdir()
        self.state = metadata / "state.json"
        self.state.write_text(json.dumps({"version": 1, "files": {}}), encoding="utf-8")

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def saved(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["files"]

    def test_status_ignores_eol_with_any_legacy_baseline_and_is_read_only(self):
        for local, remote in [(b"a\r\nb\r\n", b"a\nb\n"),
                              (b"a\nb\n", b"a\r\nb\r\n"),
                              (b"a\r\nb\n", b"a\nb\n")]:
            for baseline in [None, digest(local), digest(remote), digest(b"other\n")]:
                with self.subTest(local=local, remote=remote, baseline=baseline):
                    target = self.write("main.tex", local)
                    state_before = self.state.read_bytes()
                    before = target.stat().st_mtime_ns
                    base = {} if baseline is None else {"main.tex": baseline}
                    api = FakeAPI({"main.tex": remote})
                    self.assertEqual(sync.status(api, self.root, "paper", base)["main.tex"], "same")
                    self.assertEqual(target.read_bytes(), local)
                    self.assertEqual(target.stat().st_mtime_ns, before)
                    self.assertEqual(self.state.read_bytes(), state_before)

    def test_legacy_lf_and_crlf_baselines_classify_real_changes(self):
        for old in [b"old\ntext\n", b"old\r\ntext\r\n"]:
            for changed_side in ["local", "remote"]:
                with self.subTest(old=old, changed_side=changed_side):
                    local = b"new\r\ntext\r\n" if changed_side == "local" else b"old\ntext\n"
                    remote = b"new\ntext\n" if changed_side == "remote" else b"old\r\ntext\r\n"
                    self.write("main.tex", local)
                    rows = sync.status(FakeAPI({"main.tex": remote}), self.root, "paper",
                                       {"main.tex": digest(old)})
                    self.assertEqual(rows["main.tex"], changed_side + " modified")

    def test_real_changes_on_both_sides_remain_conflicts(self):
        self.write("main.tex", b"local\r\n")
        api = FakeAPI({"main.tex": b"remote\n"})
        base = {"main.tex": digest(b"old\r\n")}
        self.assertEqual(sync.status(api, self.root, "paper", base)["main.tex"], "conflict")
        for direction in ["push", "pull"]:
            with self.assertRaisesRegex(OverleafError, "Conflicts detected"):
                sync.run(api, self.root, "paper", base, direction)
        self.assertEqual(api.uploads, [])
        self.assertEqual((self.root / "main.tex").read_bytes(), b"local\r\n")

    def test_spaces_final_newlines_and_bom_are_not_ignored(self):
        for remote in [b"text \n", b"text", b"\xef\xbb\xbftext\n"]:
            with self.subTest(remote=remote):
                self.write("main.tex", b"text\n")
                rows = sync.status(FakeAPI({"main.tex": remote}), self.root, "paper", {})
                self.assertEqual(rows["main.tex"], "conflict")

    def test_binary_unknown_and_invalid_utf8_stay_byte_exact(self):
        for name, local, remote in [
            ("figure.pdf", b"pdf\r\n", b"pdf\n"),
            ("image.png", b"png\r\n", b"png\n"),
            ("blob.bin", b"text\r\n", b"text\n"),
            ("main.tex", b"\xff\r\n", b"\xff\n"),
            ("main.tex", b"\x00a\r\n", b"\x00a\n"),
        ]:
            with self.subTest(name=name, local=local):
                self.write(name, local)
                rows = sync.status(FakeAPI({name: remote}), self.root, "paper", {name: digest(local)})
                self.assertEqual(rows[name], "remote modified")
                self.assertEqual(sync._lf_content(name, local), local)

    def test_noop_and_force_do_not_rewrite_eol_only_files(self):
        for direction in ["push", "pull"]:
            for force in [False, True]:
                with self.subTest(direction=direction, force=force):
                    content = b"text\r\n"
                    target = self.write("main.tex", content)
                    before = target.stat().st_mtime_ns
                    api = FakeAPI({"main.tex": b"text\n"})
                    count, skipped = sync.run(api, self.root, "paper", {"main.tex": digest(content)},
                                              direction, force=force)
                    self.assertEqual((count, skipped), (0, {}))
                    self.assertEqual(api.uploads, [])
                    self.assertEqual(target.read_bytes(), content)
                    self.assertEqual(target.stat().st_mtime_ns, before)
                    self.assertEqual(self.saved()["main.tex"], digest(b"text\n"))
                    self.assertEqual(sync.status(api, self.root, "paper", self.saved())["main.tex"], "same")

    def test_push_sends_lf_and_followup_status_is_same(self):
        self.write("main.tex", b"edited\r\ntext\r\n")
        api = FakeAPI({"main.tex": b"old\n"})
        count, skipped = sync.run(api, self.root, "paper", {"main.tex": digest(b"old\r\n")}, "push")
        self.assertEqual((count, skipped), (1, {}))
        self.assertEqual(api.uploads, [("main.tex", b"edited\ntext\n")])
        self.assertEqual(self.saved()["main.tex"], digest(b"edited\ntext\n"))
        self.assertEqual((self.root / "main.tex").read_bytes(), b"edited\r\ntext\r\n")
        self.assertEqual(sync.status(api, self.root, "paper", self.saved())["main.tex"], "same")

    def test_pull_saves_lf_and_followup_status_is_same(self):
        self.write("main.tex", b"old\r\n")
        api = FakeAPI({"main.tex": b"edited\r\ntext\r\n"})
        count, skipped = sync.run(api, self.root, "paper", {"main.tex": digest(b"old\r\n")}, "pull")
        self.assertEqual((count, skipped), (1, {}))
        self.assertEqual((self.root / "main.tex").read_bytes(), b"edited\ntext\n")
        self.assertEqual(self.saved()["main.tex"], digest(b"edited\ntext\n"))
        self.assertEqual(sync.status(api, self.root, "paper", self.saved())["main.tex"], "same")

    def test_binary_transfers_preserve_bytes(self):
        for direction in ["push", "pull"]:
            with self.subTest(direction=direction):
                local, remote = ((b"new\r\n", b"old\n") if direction == "push"
                                 else (b"old\n", b"new\r\n"))
                self.write("figure.pdf", local)
                api = FakeAPI({"figure.pdf": remote})
                sync.run(api, self.root, "paper", {"figure.pdf": digest(b"old\n")}, direction)
                self.assertEqual(api.files["figure.pdf"], b"new\r\n")
                if direction == "pull":
                    self.assertEqual((self.root / "figure.pdf").read_bytes(), b"new\r\n")

    def test_force_still_transfers_real_conflicts_and_deletes_destination_only(self):
        for direction in ["push", "pull"]:
            with self.subTest(direction=direction):
                self.write("main.tex", b"local\r\n")
                self.write("local.pdf", b"local asset\r\n")
                api = FakeAPI({"main.tex": b"remote\r\n", "remote.pdf": b"remote asset\r\n"})
                base = {"main.tex": digest(b"base\n")}
                count, skipped = sync.run(api, self.root, "paper", base, direction, force=True)
                self.assertEqual((count, skipped), (3, {}))
                expected = b"local\n" if direction == "push" else b"remote\n"
                self.assertEqual(api.files["main.tex"], expected if direction == "push" else b"remote\r\n")
                if direction == "pull":
                    self.assertEqual((self.root / "main.tex").read_bytes(), expected)
                    self.assertFalse((self.root / "local.pdf").exists())
                    self.assertEqual((self.root / "remote.pdf").read_bytes(), b"remote asset\r\n")
                else:
                    self.assertNotIn("remote.pdf", api.files)
                    self.assertEqual(api.files["local.pdf"], b"local asset\r\n")
                self.assertEqual(sync.status(api, self.root, "paper", self.saved())["main.tex"], "same")
                # Keep each direction independent without touching user files.
                for name in ["local.pdf", "remote.pdf"]:
                    (self.root / name).unlink(missing_ok=True)

    def test_ordinary_transfer_preserves_destination_changes(self):
        for direction in ["push", "pull"]:
            with self.subTest(direction=direction):
                local = b"old\r\n" if direction == "push" else b"edited\r\n"
                remote = b"edited\n" if direction == "push" else b"old\n"
                self.write("main.tex", local)
                api = FakeAPI({"main.tex": remote})
                count, skipped = sync.run(api, self.root, "paper", {"main.tex": digest(b"old\n")}, direction)
                self.assertEqual(count, 0)
                self.assertEqual(skipped, {"main.tex": "remote modified" if direction == "push" else "local modified"})
                self.assertEqual((self.root / "main.tex").read_bytes(), local)
                self.assertEqual(api.files["main.tex"], remote)

    def test_concurrent_eol_edit_still_stops_push(self):
        target = self.write("main.tex", b"edited\r\n")
        api = FakeAPI({"main.tex": b"old\n"}, on_batch=lambda: target.write_bytes(b"edited\n"))
        with self.assertRaisesRegex(OverleafError, "Local file changed during synchronization"):
            sync.run(api, self.root, "paper", {"main.tex": digest(b"old\n")}, "push")
        self.assertEqual(api.uploads, [])
        self.assertEqual(self.saved(), {})

    def test_legacy_mixed_baseline_keeps_real_remote_change(self):
        old = b"old\r\ntext\n"
        self.write("main.tex", old)
        rows = sync.status(FakeAPI({"main.tex": b"changed\ntext\n"}), self.root, "paper",
                           {"main.tex": digest(old)})
        self.assertEqual(rows["main.tex"], "remote modified")

    def test_unrecoverable_legacy_mixed_baseline_stays_conservative(self):
        self.write("main.tex", b"old\ntext\n")
        rows = sync.status(FakeAPI({"main.tex": b"changed\ntext\n"}), self.root, "paper",
                           {"main.tex": digest(b"old\r\ntext\n")})
        self.assertEqual(rows["main.tex"], "conflict")

    def test_missing_files_match_legacy_baseline_across_eol(self):
        self.write("local.tex", b"old\n")
        api = FakeAPI({"remote.tex": b"old\r\n"})
        base = {"local.tex": digest(b"old\r\n"), "remote.tex": digest(b"old\n")}
        rows = sync.status(api, self.root, "paper", base)
        self.assertEqual(rows["local.tex"], "missing remote")
        self.assertEqual(rows["remote.tex"], "missing local")

    def test_normalized_zip_entry_names_remain_supported(self):
        self.write("sec/main.tex", b"text\r\n")
        rows = sync.status(FakeAPI({"sec//main.tex": b"text\n"}), self.root, "paper", {})
        self.assertEqual(rows["sec/main.tex"], "same")

    def test_clone_uses_lf_for_text_and_preserves_binary(self):
        destination = Path(self.temporary.name).resolve() / "clone"
        api = FakeAPI({"main.tex": b"text\r\n", "figure.pdf": b"pdf\r\n"})
        sync.clone(api, "Paper", destination)
        self.assertEqual((destination / "main.tex").read_bytes(), b"text\n")
        self.assertEqual((destination / "figure.pdf").read_bytes(), b"pdf\r\n")
        state = json.loads((destination / ".leafbridge/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["files"]["main.tex"], digest(b"text\n"))
        self.assertEqual(state["files"]["figure.pdf"], digest(b"pdf\r\n"))


if __name__ == "__main__":
    unittest.main()