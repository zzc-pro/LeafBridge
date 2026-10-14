"""登录和会话的离线测试，不连接真实账户。"""

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
    return {"version": 1, "base_url": client.BASE_URL, "csrf": "test-csrf",
            "cookies": [{"name": "overleaf_session2", "value": "test-session",
                         "domain": ".overleaf.com", "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"}]}


class BrowserError(Exception):
    pass


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config" / "session.json"
        self.client = client.OverleafClient(self.path)

    def browser(self):
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

    def test_save_and_load_session_preserves_cookie_metadata(self):
        self.assertEqual(self.client._save_session(session()), self.path)
        self.assertEqual(self.client.load_session(), session())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_missing_and_broken_session(self):
        with self.assertRaisesRegex(client.LoginError, "Not logged in"):
            self.client.load_session()
        self.path.parent.mkdir()
        for text in ["not-json", "[]", json.dumps({"version": 2}), "null"]:
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(client.LoginError):
                self.client.load_session()

    def test_expired_cookie_is_rejected(self):
        for expires in [0, 1]:
            data = session()
            data["cookies"][0]["expires"] = expires
            with self.assertRaisesRegex(client.LoginError, "expired"):
                client._validate_session(data)

    def test_non_session_cookies_do_not_prove_login(self):
        data = session()
        data["cookies"][0]["name"] = "GCLB"
        with self.assertRaises(client.LoginError):
            client._validate_session(data)

    def test_other_domains_and_invalid_cookie_fields_are_rejected(self):
        for field, value in [("domain", "evil-overleaf.com"), ("domain", "idp.example.com"),
                             ("value", None), ("expires", "forever"),
                             ("expires", float("nan")), ("path", "relative")]:
            data = session()
            data["cookies"][0][field] = value
            with self.assertRaises(client.LoginError):
                client._validate_session(data)

    def test_expired_auxiliary_cookies_are_dropped(self):
        data = session()
        expired = deepcopy(data["cookies"][0])
        expired.update(name="GCLB", expires=1)
        data["cookies"].append(expired)
        self.assertEqual(client._validate_session(data), session())

    def test_failed_atomic_save_preserves_previous_session(self):
        self.client._save_session(session())
        changed = session()
        changed["csrf"] = "new-csrf"
        with patch.object(client.os, "replace", side_effect=OSError("denied")):
            with self.assertRaisesRegex(client.LoginError, "could not be saved"):
                self.client._save_session(changed)
        self.assertEqual(self.client.load_session(), session())
        self.assertEqual(list(self.path.parent.glob(".session-*")), [])

    def test_successful_browser_login_saves_and_closes(self):
        factory, playwright, context, page = self.browser()
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)):
            self.assertEqual(self.client.login(), self.path)
        self.assertEqual(self.client.load_session(), session())
        options = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["channel"], "chrome")
        self.assertFalse(options["headless"])
        context.cookies.assert_called_once_with(client.BASE_URL + "/project")
        context.close.assert_called_once()
        self.assertFalse(Path(playwright.chromium.launch_persistent_context.call_args.args[0]).exists())

    def test_default_chrome_failure_does_not_fall_back_or_save(self):
        factory, playwright, context, page = self.browser()
        playwright.chromium.launch_persistent_context.side_effect = BrowserError("not found")
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "Unable to launch the browser"):
                self.client.login()
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_auto_browser_falls_back(self):
        factory, playwright, context, page = self.browser()
        playwright.chromium.launch_persistent_context.side_effect = [BrowserError(), context]
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)):
            self.client.login(browser="auto")
        self.assertEqual(playwright.chromium.launch_persistent_context.call_count, 2)

    def test_timeout_preserves_previous_session(self):
        self.client._save_session(session())
        factory, playwright, context, page = self.browser()
        page.url = client.BASE_URL + "/login"
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)), \
                patch.object(client.time, "monotonic", side_effect=[0, 0.1, 2]):
            with self.assertRaisesRegex(client.LoginError, "timed out"):
                self.client.login(timeout=1)
        self.assertEqual(self.client.load_session(), session())
        context.close.assert_called_once()

    def test_foreign_project_page_is_not_accepted(self):
        factory, playwright, context, page = self.browser()
        page.url = "https://idp.example.com/project"
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)), \
                patch.object(client.time, "monotonic", side_effect=[0, 0.1, 2]):
            with self.assertRaisesRegex(client.LoginError, "timed out"):
                self.client.login(timeout=1)
        page.locator.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_closing_window_is_reported(self):
        factory, playwright, context, page = self.browser()
        page.goto.side_effect = lambda *args, **kwargs: setattr(context, "pages", [])
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "login window was closed"):
                self.client.login()
        self.assertFalse(self.path.exists())
        context.close.assert_called_once()

    def test_server_error_is_reported(self):
        factory, playwright, context, page = self.browser()
        page.goto.return_value.status = 503
        with patch.object(client, "_load_playwright", return_value=(factory, BrowserError)):
            with self.assertRaisesRegex(client.LoginError, "temporarily unavailable"):
                self.client.login()
        self.assertFalse(self.path.exists())
        context.close.assert_called_once()

    def test_timeout_validation_runs_before_browser_import(self):
        for value in [0, -1, float("nan"), float("inf"), True]:
            with patch.object(client, "_load_playwright") as loader:
                with self.assertRaises(client.LoginError):
                    self.client.login(timeout=value)
                loader.assert_not_called()

    def test_browser_search_skips_inaccessible_directories(self):
        with patch.object(client.sys, "platform", "win32"), \
                patch.object(client.shutil, "which", return_value=None), \
                patch.object(client.Path, "is_file", side_effect=[PermissionError(), True]):
            self.assertTrue(client._browser_executable("msedge").endswith("msedge.exe"))

    def test_installed_browser_can_be_found_on_path(self):
        with patch.object(client.shutil, "which", return_value="test-browser-executable"):
            self.assertEqual(client._browser_executable("msedge"), "test-browser-executable")
            self.assertEqual(client._browser_executable("chrome"), "test-browser-executable")
            self.assertIsNone(client._browser_executable("chromium"))

    def test_windows_config_path_respects_appdata(self):
        with patch.object(client.sys, "platform", "win32"), \
                patch.dict(os.environ, {"APPDATA": self.directory.name}):
            self.assertEqual(client.default_session_path(), Path(self.directory.name) / "leafbridge" / "session.json")

    def test_cli_success_does_not_print_credentials(self):
        output = io.StringIO()
        with patch.object(cli.OverleafClient, "login", return_value=self.path) as login, redirect_stdout(output):
            self.assertEqual(cli.main(["login", "--timeout", "600"]), 0)
        login.assert_called_once_with(browser="chrome", timeout=600)
        self.assertIn("Login successful", output.getvalue())
        self.assertNotIn("test-session", output.getvalue())
        self.assertNotIn("test-csrf", output.getvalue())

    def test_cli_failure_and_cancel_return_nonzero(self):
        for error, expected in [(client.LoginError("Test failure"), 1), (KeyboardInterrupt(), 130)]:
            with patch.object(cli.OverleafClient, "login", side_effect=error), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["login"]), expected)

    def test_cli_rejects_invalid_timeout(self):
        for value in ["0", "-1", "nan", "inf", "invalid"]:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.main(["login", "--timeout", value])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
