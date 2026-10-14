"""Offline tests for the upload root folder lookup and Socket.IO packet framing."""

import json
import unittest
from unittest.mock import MagicMock, patch

from leafbridge import client
from leafbridge.client import OverleafError


def response(text, status=200):
    """Build a fake HTTP response with a text payload."""
    result = MagicMock()
    result.status = status
    result.text.return_value = text
    return result


def event(project_id="project-id", folders=None):
    """Build a fake automatic project-join event without real account information."""
    folders = [{"_id": "root-id"}] if folders is None else folders
    return "5:::" + json.dumps({"name": "joinProjectResponse", "args": [{"project": {
        "_id": project_id, "rootFolder": folders}}]})


class RootTests(unittest.TestCase):
    def setUp(self):
        """Mock a supported HTTP polling handshake and project metadata response."""
        self.api = client.OverleafClient()
        self.request = MagicMock()
        self.handshake = response("fake-session:60:60:websocket,xhr-polling")
        self.request.get.side_effect = [self.handshake, response("1::"), response(event())]

    def test_root(self):
        """Retrieve and cache the root folder while closing the transient connection."""
        self.assertEqual(self.api._root(self.request, "project-id"), "root-id")
        self.request.get.assert_any_call("/socket.io/1/", params={"projectId": "project-id"},
                                         max_redirects=0, timeout=30000)
        self.request.post.assert_called_once_with("/socket.io/1/xhr-polling/fake-session", data="0::",
                           headers={"Content-Type": "text/plain"}, max_redirects=0, timeout=5000)
        self.assertEqual(self.api._root(self.request, "project-id"), "root-id")
        self.assertEqual(self.request.get.call_count, 3)

    def test_frames(self):
        """Decode batched packets using UTF-16 lengths, even with emoji and separator characters."""
        payload = '5:::{"name":"example","text":"blue leaf 🍃 \ufffd"}'
        frames = ["1::", payload]
        text = "".join("\ufffd" + str(len(frame.encode("utf-16-le")) // 2) + "\ufffd" + frame for frame in frames)
        self.assertEqual(client._frames(text), frames)
        self.assertEqual(client._frames("1::"), ["1::"])
        for text in ["\ufffdwrong\ufffd1::", "\ufffd100\ufffd1::", "\ufffd0\ufffd", "\ufffd3\ufffd1::extra"]:
            with self.subTest(text=text), self.assertRaises(OverleafError):
                client._frames(text)

    def test_batch(self):
        """Read a batched join event arriving with the initial connection packet."""
        text = "".join("\ufffd" + str(len(frame)) + "\ufffd" + frame for frame in ["1::", event()])
        self.request.get.side_effect = [self.handshake, response(text)]
        self.assertEqual(self.api._root(self.request, "project-id"), "root-id")
        self.assertEqual(self.request.get.call_count, 2)

    def test_errors(self):
        """Reject failed handshakes and unsupported transports without exposing the session ID."""
        for text, status in [("private-cookie", 403), ("invalid", 200),
                             ("private-session:60:60:websocket", 200)]:
            self.request.get.side_effect = [response(text, status)]
            with self.subTest(status=status, text=text), self.assertRaises(OverleafError) as error:
                self.api._root(self.request, "project-id")
            self.assertNotIn("private-cookie", str(error.exception))
            self.assertNotIn("private-session", str(error.exception))
        self.assertEqual(self.api._roots, {})

    def test_metadata(self):
        """Reject malformed, unauthorized, and mismatched project root metadata."""
        for text in ["0::", "7:::not authorized", "5:::invalid JSON", "5:::[]",
                     '5:::{"name":"connectionRejected","args":["private detail"]}',
                     event("other-project"), event(folders=[]), event(folders=[{"_id": None}])]:
            self.request.get.side_effect = [self.handshake, response(text)]
            with self.subTest(text=text), self.assertRaises(OverleafError) as error:
                self.api._root(self.request, "project-id")
            self.assertNotIn("private detail", str(error.exception))
        self.assertEqual(self.api._roots, {})

    def test_timeout(self):
        """Limit unsuccessful metadata polling and disconnect without caching a result."""
        self.request.get.side_effect = [self.handshake] + [response("1::")] * 6
        with self.assertRaisesRegex(OverleafError, "Timed out"):
            self.api._root(self.request, "project-id")
        self.assertEqual(self.request.get.call_count, 7)
        self.assertEqual(self.api._roots, {})
        self.request.post.assert_called_once()

    def test_close(self):
        """Preserve successful metadata lookup when best-effort disconnection fails."""
        self.request.post.side_effect = RuntimeError("private-session")
        self.assertEqual(self.api._root(self.request, "project-id"), "root-id")
