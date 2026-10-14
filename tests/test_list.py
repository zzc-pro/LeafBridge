"""Offline project query and CLI tests; no real credentials or network access."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from leafbridge import client, cli
from test_login import BrowserError, session


def response(projects, total=None, status=200):
    """Build a mocked project response with a status and total count."""
    result = MagicMock()
    result.status = status
    result.json.return_value = {"projects": projects, "totalSize": len(projects) if total is None else total}
    return result


class ListTests(unittest.TestCase):
    def setUp(self):
        """Configure mocked sessions and HTTP responses for each test."""
        self.api = client.OverleafClient()
        session_patch = patch.object(self.api, "load", return_value=session())
        self.load = session_patch.start()
        self.addCleanup(session_patch.stop)
        self.playwright, self.request = MagicMock(), MagicMock()
        self.playwright.request.new_context.return_value = self.request
        manager = MagicMock()
        manager.__enter__.return_value = self.playwright
        loader_patch = patch.object(client, "_playwright", return_value=(lambda: manager, BrowserError))
        self.loader = loader_patch.start()
        self.addCleanup(loader_patch.stop)

    def test_active(self):
        """Return active owned and shared projects without opening a browser."""
        projects = [{"id": "a", "name": "Owned", "accessLevel": "owner"},
                    {"id": "b", "name": "Shared", "accessLevel": "readOnly"},
                    {"id": "c", "name": "Archived", "archived": True},
                    {"id": "d", "name": "Trash", "trashed": True}]
        self.request.post.return_value = response(projects)
        self.assertEqual(self.api.projects(), projects[:2])
        self.playwright.chromium.launch_persistent_context.assert_not_called()
        options = self.playwright.request.new_context.call_args.kwargs
        self.assertEqual(options["storage_state"]["cookies"], session()["cookies"])
        self.assertEqual(options["extra_http_headers"]["X-CSRF-Token"], session()["csrf"])
        self.assertEqual(options["timeout"], 30_000)
        self.assertEqual(self.request.post.call_args.kwargs["data"]["filters"], {})
        self.assertEqual(self.request.post.call_args.kwargs["max_redirects"], 0)
        self.request.dispose.assert_called_once()

    def test_pages(self):
        """Follow pagination cursors and exclude archived entries after retrieval."""
        first = [{"id": "a", "name": "First", "archived": True}, {"id": "b", "name": "Second"}]
        second = [{"id": "c", "name": "Third"}]
        self.request.post.side_effect = [response(first, 3), response(second, 3)]
        self.assertEqual(self.api.projects(), first[1:] + second)
        pages = [call.kwargs["data"]["page"] for call in self.request.post.call_args_list]
        self.assertEqual(pages, [{"size": 100}, {"size": 100, "lastId": "b"}])

    def test_repeated_page(self):
        """Reject repeated pages instead of reporting an incomplete list as success."""
        self.request.post.return_value = response([{"id": "a", "name": "First"}], 2)
        with self.assertRaisesRegex(client.OverleafError, "incomplete"):
            self.api.projects()
        self.assertEqual(self.request.post.call_count, 2)
        self.request.dispose.assert_called_once()

    def test_empty_page(self):
        """Reject empty pages when the server reports remaining projects."""
        self.request.post.return_value = response([], 1)
        with self.assertRaisesRegex(client.OverleafError, "incomplete"):
            self.api.projects()

    def test_changed_count(self):
        """Reject project totals that change during pagination."""
        self.request.post.side_effect = [response([{"id": "a", "name": "First"}], 2),
                                         response([{"id": "b", "name": "Second"}], 3)]
        with self.assertRaisesRegex(client.OverleafError, "changed during retrieval"):
            self.api.projects()

    def test_empty(self):
        """Treat an empty account as a successful query."""
        self.request.post.return_value = response([])
        self.assertEqual(self.api.projects(), [])

    def test_auth(self):
        """Request a new login after redirects or authentication failures."""
        for status in [302, 401, 403]:
            with self.subTest(status=status):
                self.request.post.return_value = response([], status=status)
                with self.assertRaisesRegex(client.LoginError, "leaf login"):
                    self.api.projects()

    def test_http_errors(self):
        """Report rate limits and server errors."""
        for status, message in [(429, "rate limit"), (500, "HTTP 500")]:
            with self.subTest(status=status):
                self.request.post.return_value = response([], status=status)
                with self.assertRaisesRegex(client.OverleafError, message):
                    self.api.projects()

    def test_html(self):
        """Reject HTML responses rather than reporting an empty account."""
        self.request.post.return_value = response([])
        self.request.post.return_value.json.side_effect = ValueError("HTML response")
        with self.assertRaisesRegex(client.OverleafError, "project JSON"):
            self.api.projects()

    def test_bad_data(self):
        """Reject malformed project response data."""
        for payload in [[], {}, {"projects": [], "totalSize": True},
                        {"projects": [], "totalSize": -1},
                        {"projects": [{"name": "Missing ID"}], "totalSize": 1}]:
            with self.subTest(payload=payload):
                self.request.post.return_value = response([])
                self.request.post.return_value.json.return_value = payload
                with self.assertRaisesRegex(client.OverleafError, "Unexpected"):
                    self.api.projects()

    def test_no_session(self):
        """Check for a login session before creating a request context."""
        self.load.side_effect = client.LoginError("Not logged in. Run leaf login.")
        with self.assertRaises(client.LoginError):
            self.api.projects()
        self.loader.assert_not_called()

    def test_network(self):
        """Report network failures without exposing request credentials."""
        self.request.post.side_effect = BrowserError("Cookie: private-test-cookie")
        with self.assertRaisesRegex(client.OverleafError, "Check your connection") as error:
            self.api.projects()
        self.assertNotIn("private-test-cookie", str(error.exception))
        self.request.dispose.assert_called_once()

    def test_cli(self):
        """Print the active project table without starting another login."""
        projects = [{"id": "a", "name": "First"}, {"id": "b", "name": "Second"}]
        output = io.StringIO()
        with patch.object(cli.OverleafClient, "projects", return_value=projects), \
                patch.object(cli.OverleafClient, "login") as login, redirect_stdout(output):
            self.assertEqual(cli.main(["list"]), 0)
        login.assert_not_called()
        self.assertIn("STATUS", output.getvalue())
        for value in ["First", "Second", "active", "Projects: 2"]:
            self.assertIn(value, output.getvalue())
        self.assertNotIn("archived", output.getvalue())
        self.assertNotIn("trashed", output.getvalue())

    def test_cli_empty(self):
        """Print an English message when no active projects are available."""
        output = io.StringIO()
        with patch.object(cli.OverleafClient, "projects", return_value=[]), redirect_stdout(output):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertEqual(output.getvalue(), "No projects found.\n")

    def test_cli_failure(self):
        """Report a failed query without printing a partial table."""
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli.OverleafClient, "projects", side_effect=client.OverleafError("Test failure")), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(["list"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Listing projects failed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
