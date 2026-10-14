"""Offline login and session tests that never connect to a real account."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from leafbridge import client, cli


def session():
    """Build a fake authentication session for offline tests."""
    return {"version": 1, "base_url": client.BASE_URL, "csrf": "test-csrf",
            "cookies": [{"name": "overleaf_session2", "value": "test-session",
                         "domain": ".overleaf.com", "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"}]}


class BrowserError(Exception):
    pass


class LoginTests(unittest.TestCase):
    def setUp(self):
        """Create isolated session storage for each test."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config" / "session.json"
        self.client = client.OverleafClient(self.path)

    def browser(self):
        """Create a mocked successful browser login context."""
        context, page, playwright = MagicMock(), MagicMock(), MagicMock()
        context.pages = [page]
        page.url = client.BASE_URL + "/project?logged_in=true"
        page.goto.return_value.status = 200
        page.locator.return_value.first.get_attribute.return_value = "test-csrf"
        context.cookies.return_value = session()["cookies"]
        playwright.chromium.launch_persistent_context.return_value = context
        manager = MagicMock()
        manager.__enter__.return_value = playwright
        factory = MagicMock(return_value=manager)
        return factory, playwright, context, page

    def test_session(self):
        """Save and load cookie metadata without losing permissions."""
        self.assertEqual(self.client._save(session()), self.path)
        self.assertEqual(self.client.load(), session())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_bad_session(self):
        """Reject missing, malformed, or unsupported session files."""
        with self.assertRaisesRegex(client.LoginError, "Not logged in"):
            self.client.load()
        self.path.parent.mkdir()
        for text in ["not-json", "[]", json.dumps({"version": 2}), "null"]:
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(client.LoginError):
                self.client.load()

    def test_expired(self):
        """Reject expired authentication cookies."""
        for expires in [0, 1]:
            data = session()
            data["cookies"][0]["expires"] = expires
            with self.assertRaisesRegex(client.LoginError, "expired"):
                client._validate(data)

    def test_no_session(self):
        """Require an authentication cookie rather than only auxiliary cookies."""
        data = session()
        data["cookies"][0]["name"] = "GCLB"
        with self.assertRaises(client.LoginError):
            client._validate(data)

    def test_bad_cookie(self):
        """Reject foreign cookie domains and malformed cookie fields."""
        for field, value in [("domain", "evil-overleaf.com"), ("domain", "idp.example.com"),
                             ("value", None), ("expires", "forever"),
                             ("expires", float("nan")), ("path", "relative")]:
            data = session()
            data["cookies"][0][field] = value
            with self.assertRaises(client.LoginError):
                client._validate(data)

    def test_cookie_cleanup(self):
        """Remove expired auxiliary cookies while retaining the login session."""
        data = session()
        expired = deepcopy(data["cookies"][0])
        expired.update(name="GCLB", expires=1)
        data["cookies"].append(expired)
        self.assertEqual(client._validate(data), session())

    def test_save_failure(self):
        """Preserve the previous session when an atomic save fails."""
        self.client._save(session())
        changed = session()
        changed["csrf"] = "new-csrf"
        with patch.object(client.os, "replace", side_effect=OSError("denied")):
            with self.assertRaisesRegex(client.LoginError, "could not be saved"):
                self.client._save(changed)
        self.assertEqual(self.client.load(), session())
        self.assertEqual(list(self.path.parent.glob(".session-*")), [])

    def test_login(self):
        """Save a successful Chrome login and close its temporary browser profile."""
        factory, playwright, context, page = self.browser()
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)):
            self.assertEqual(self.client.login(), self.path)
        self.assertEqual(self.client.load(), session())
        options = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["channel"], "chrome")
        self.assertFalse(options["headless"])
        context.cookies.assert_called_once_with(client.BASE_URL + "/project")
        context.close.assert_called_once()
        self.assertFalse(Path(playwright.chromium.launch_persistent_context.call_args.args[0]).exists())

    def test_chrome_failure(self):
        """Report default Chrome startup failure without saving or switching browsers."""
        factory, playwright, context, page = self.browser()
        playwright.chromium.launch_persistent_context.side_effect = BrowserError("not found")
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "Unable to launch the browser"):
                self.client.login()
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_fallback(self):
        """Try another browser when automatic browser selection fails."""
        factory, playwright, context, page = self.browser()
        playwright.chromium.launch_persistent_context.side_effect = [BrowserError(), context]
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)):
            self.client.login(browser="auto")
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 2)

    def test_timeout(self):
        """Preserve the previous saved session after a login timeout."""
        self.client._save(session())
        factory, playwright, context, page = self.browser()
        page.url = client.BASE_URL + "/login"
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)), \
                patch.object(client.time, "monotonic", side_effect=[0, 0.1, 2]):
            with self.assertRaisesRegex(client.LoginError, "timed out"):
                self.client.login(timeout=1)
        self.assertEqual(self.client.load(), session())
        context.close.assert_called_once()

    def test_foreign_page(self):
        """Ignore project pages hosted outside the Overleaf origin."""
        factory, playwright, context, page = self.browser()
        page.url = "https://idp.example.com/project"
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)), \
                patch.object(client.time, "monotonic", side_effect=[0, 0.1, 2]):
            with self.assertRaisesRegex(client.LoginError, "timed out"):
                self.client.login(timeout=1)
        page.locator.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_closed(self):
        """Report window closure without saving a session."""
        factory, playwright, context, page = self.browser()
        page.goto.side_effect = lambda *args, **kwargs: setattr(context, "pages", [])
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "login window was closed"):
                self.client.login()
        self.assertFalse(self.path.exists())
        context.close.assert_called_once()

    def test_server_error(self):
        """Report an unavailable Overleaf server."""
        factory, playwright, context, page = self.browser()
        page.goto.return_value.status = 503
        with patch.object(client, "_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "temporarily unavailable"):
                self.client.login()
        self.assertFalse(self.path.exists())
        context.close.assert_called_once()

    def test_bad_timeout(self):
        """Validate timeout values before loading browser dependencies."""
        for value in [0, -1, float("nan"), float("inf"), True]:
            with patch.object(client, "_playwright") as loader:
                with self.assertRaises(client.LoginError):
                    self.client.login(timeout=value)
                loader.assert_not_called()

    def test_browser_access(self):
        """Skip inaccessible directories when searching for browsers."""
        with patch.object(client.sys, "platform", "win32"), \
                patch.object(client.shutil, "which", return_value=None), \
                patch.object(client.Path, "is_file", side_effect=[PermissionError(), True]):
            self.assertTrue(client._browser_path("msedge").endswith("msedge.exe"))

    def test_browser_path(self):
        """Find installed browsers through PATH without changing Chromium selection."""
        with patch.object(client.shutil, "which", return_value="test-browser-executable"):
            self.assertEqual(client._browser_path("msedge"), "test-browser-executable")
            self.assertEqual(client._browser_path("chrome"), "test-browser-executable")
            self.assertIsNone(client._browser_path("chromium"))

    def test_config_path(self):
        """Use APPDATA to locate the Windows session file."""
        with patch.object(client.sys, "platform", "win32"), \
                patch.dict(os.environ, {"APPDATA": self.directory.name}):
            self.assertEqual(client.session_file(), Path(self.directory.name) / "leafbridge" / "session.json")

    def test_cli_login(self):
        """Default to Chrome and report CLI login success without exposing credentials."""
        output = io.StringIO()
        with patch.object(cli.OverleafClient, "login", return_value=self.path) as login, redirect_stdout(output):
            self.assertEqual(cli.main(["login", "--timeout", "600"]), 0)
        login.assert_called_once_with(browser="chrome", timeout=600)
        self.assertIn("Login successful", output.getvalue())
        self.assertNotIn("test-session", output.getvalue())
        self.assertNotIn("test-csrf", output.getvalue())

    def test_cli_failure(self):
        """Return nonzero exit codes for failed or cancelled CLI login."""
        for error, expected in [(client.LoginError("Test failure"), 1), (KeyboardInterrupt(), 130)]:
            with patch.object(cli.OverleafClient, "login", side_effect=error), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["login"]), expected)

    def test_cli_timeout(self):
        """Reject invalid CLI timeout values."""
        for value in ["0", "-1", "nan", "inf", "invalid"]:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(["login", "--timeout", value])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
