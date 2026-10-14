"""Overleaf browser login, session storage, and project queries."""

from contextlib import contextmanager
import json
import math
import os
import shutil
from pathlib import Path, PurePosixPath
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit

BASE_URL = "https://www.overleaf.com"
SESSION_COOKIES = {"overleaf_session2", "sharelatex.sid"}


class OverleafError(RuntimeError):
    """An Overleaf operation error suitable for display to the user."""


class LoginError(OverleafError):
    """A login error suitable for display to the user."""


def session_file():
    """Store the session in the user configuration directory, outside Git repositories."""
    if sys.platform == "win32":
        directory = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support"
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return directory / "leafbridge" / "session.json"


def _validate(data):
    """Validate session fields and discard expired cookies."""
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("base_url") != BASE_URL:
        raise LoginError("Unsupported session format. Run leaf login again.")
    csrf = data.get("csrf")
    cookies = data.get("cookies")
    if not isinstance(csrf, str) or not csrf.strip() or not isinstance(cookies, list):
        raise LoginError("Incomplete session. Run leaf login again.")
    valid_cookies = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise LoginError("Invalid session cookie format. Run leaf login again.")
        name, value = cookie.get("name"), cookie.get("value")
        domain, path = cookie.get("domain"), cookie.get("path")
        expires = cookie.get("expires", -1)
        if (not isinstance(name, str) or not name or not isinstance(value, str)
                or not isinstance(domain, str) or domain.lstrip(".") not in {"overleaf.com", "www.overleaf.com"}
                or not isinstance(path, str) or not path.startswith("/")
                or isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)):
            raise LoginError("Invalid session cookie format. Run leaf login again.")
        if expires != -1 and expires <= time.time():
            continue
        valid_cookies.append(cookie)
    if not any(c["name"] in SESSION_COOKIES and c["value"] for c in valid_cookies):
        raise LoginError("Session is missing or expired. Run leaf login again.")
    return {**data, "cookies": valid_cookies}


def _playwright():
    """Load Playwright only when a browser or HTTP operation needs it."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise LoginError("Playwright dependency is missing. Run python -m pip install -e . in the leafbridge Conda environment.") from None
    return sync_playwright, Error


def _frames(text):
    """Decode single or length-prefixed Socket.IO packets using JavaScript string lengths."""
    if not text.startswith("\ufffd"):
        return [text]
    frames = []
    try:
        while text:
            if not text.startswith("\ufffd"):
                raise ValueError
            length, body = text[1:].split("\ufffd", 1)
            size = int(length) * 2
            data = body.encode("utf-16-le")
            if size <= 0 or size > len(data):
                raise ValueError
            frames.append(data[:size].decode("utf-16-le"))
            text = data[size:].decode("utf-16-le")
    except (ValueError, UnicodeError):
        raise OverleafError("Unexpected project metadata packet. Check Overleaf API compatibility.") from None
    return frames


def _browser_path(channel):
    """Find an installed browser instead of relying only on Playwright's default paths."""
    if channel == "chromium":
        return None
    commands = {"msedge": ["msedge", "microsoft-edge", "microsoft-edge-stable"],
                "chrome": ["chrome", "google-chrome", "google-chrome-stable"]}
    for command in commands[channel]:
        try:
            executable = shutil.which(command)
        except OSError:
            continue
        if executable:
            return executable
    if sys.platform == "win32":
        relative = ("Microsoft/Edge/Application/msedge.exe" if channel == "msedge"
                    else "Google/Chrome/Application/chrome.exe")
        directories = [os.environ.get("PROGRAMFILES", "C:/Program Files"),
                       os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
                       os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))]
        candidates = [Path(directory) / relative for directory in directories]
    elif sys.platform == "darwin":
        relative = ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge" if channel == "msedge"
                    else "Google Chrome.app/Contents/MacOS/Google Chrome")
        candidates = [Path("/Applications") / relative, Path.home() / "Applications" / relative]
    else:
        candidates = []
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


class OverleafClient:
    def __init__(self, session_path=None):
        """Use the default user session file unless a custom path is provided."""
        self.session_path = Path(session_path) if session_path is not None else session_file()
        self._roots = {}
        self._request = None

    def load(self):
        """Read and validate the session; the server may revoke cookies before expiry."""
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LoginError("Not logged in. Run leaf login.") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise LoginError("Unable to read the session. Run leaf login again.") from None
        return _validate(data)

    def _save(self, data):
        """Validate and atomically save session data, preserving the previous file on failure."""
        data = _validate(data)
        directory = self.session_path.parent
        temporary_path = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=directory)
            temporary_path = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.session_path)
        except OSError:
            raise LoginError("Login completed, but the session could not be saved. Check write permissions for the user configuration directory.") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return self.session_path

    def projects(self):
        """Query active projects using the saved session without launching a browser."""
        session = self.load()
        sync_playwright, request_error = _playwright()
        try:
            with sync_playwright() as playwright:
                request = playwright.request.new_context(
                    base_url=BASE_URL,
                    storage_state={"cookies": session["cookies"], "origins": []},
                    extra_http_headers={"X-CSRF-Token": session["csrf"], "Origin": BASE_URL,
                                        "Referer": BASE_URL + "/project", "Accept": "application/json"},
                    timeout=30_000,
                )
                try:
                    projects, seen = [], set()
                    page = {"size": 100}
                    expected_total = None
                    while True:
                        response = request.post(
                            "/api/project", max_redirects=0,
                            data={"filters": {}, "sort": {"by": "lastUpdated", "order": "desc"}, "page": page},
                        )
                        if response.status == 401 or 300 <= response.status < 400:
                            raise LoginError("Session is no longer authenticated. Run leaf login again.")
                        if response.status == 403:
                            raise LoginError("Overleaf denied the request. Run leaf login again to renew the session and browser verification.")
                        if response.status == 429:
                            raise OverleafError("Overleaf rate limit reached. Try again later.")
                        if response.status != 200:
                            raise OverleafError(f"Project listing failed (HTTP {response.status}).")
                        try:
                            payload = response.json()
                        except ValueError:
                            raise OverleafError("Overleaf did not return project JSON. Check login or API compatibility.") from None
                        if (not isinstance(payload, dict) or not isinstance(payload.get("projects"), list)
                                or type(payload.get("totalSize")) is not int or payload["totalSize"] < 0):
                            raise OverleafError("Unexpected project response format. Overleaf may have changed its API.")
                        total = payload["totalSize"]
                        if expected_total is not None and total != expected_total:
                            raise OverleafError("The project list changed during retrieval. Run leaf list again.")
                        expected_total = total
                        previous_count = len(projects)
                        batch = payload["projects"]
                        for item in batch:
                            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                                    or not isinstance(item.get("name"), str)
                                    or any(key in item and not isinstance(item[key], bool) for key in ("archived", "trashed"))):
                                raise OverleafError("Unexpected project data. Overleaf may have changed its API.")
                            if item["id"] not in seen:
                                seen.add(item["id"])
                                projects.append({key: item[key] for key in
                                                 ("id", "name", "archived", "trashed", "lastUpdated", "accessLevel")
                                                 if key in item})
                        if len(projects) > total:
                            raise OverleafError("Project count does not match the server response. Run leaf list again.")
                        if len(projects) == total:
                            return [project for project in projects
                                    if not project.get("archived") and not project.get("trashed")]
                        if len(projects) == previous_count or not batch or page.get("lastId") == batch[-1]["id"]:
                            raise OverleafError("Overleaf returned an incomplete project list. Pagination did not advance.")
                        page = {"size": 100, "lastId": batch[-1]["id"]}
                finally:
                    try:
                        request.dispose()
                    except request_error:
                        pass
        except request_error:
            raise OverleafError("Unable to query Overleaf. Check your connection and try again.") from None

    def download(self, project_id):
        """Download a project's source ZIP using the saved login session."""
        session = self.load()
        sync_playwright, request_error = _playwright()
        try:
            with sync_playwright() as playwright:
                request = playwright.request.new_context(
                    base_url=BASE_URL,
                    storage_state={"cookies": session["cookies"], "origins": []},
                    extra_http_headers={"Accept": "application/zip", "Referer": BASE_URL + "/project"},
                    timeout=60_000,
                )
                try:
                    response = request.get(f"/Project/{quote(project_id, safe='')}/download/zip", max_redirects=0)
                    if response.status == 401 or 300 <= response.status < 400:
                        raise LoginError("Session is no longer authenticated. Run leaf login again.")
                    if response.status == 403:
                        raise OverleafError("Overleaf denied the download. Check project access or run leaf login again.")
                    if response.status == 429:
                        raise OverleafError("Overleaf rate limit reached. Try again later.")
                    if response.status != 200:
                        raise OverleafError(f"Project download failed (HTTP {response.status}).")
                    return response.body()
                finally:
                    try:
                        request.dispose()
                    except request_error:
                        pass
        except request_error:
            raise OverleafError("Unable to download the project. Check your connection and try again.") from None

    def _root(self, request, project_id):
        """Read and cache the project root folder ID through a short-lived HTTP polling connection."""
        if project_id in self._roots:
            return self._roots[project_id]
        handshake = request.get("/socket.io/1/", params={"projectId": project_id},
                                max_redirects=0, timeout=30_000)
        if handshake.status != 200:
            raise OverleafError(f"Unable to read upload folder metadata (HTTP {handshake.status}). Check login and project access.")
        parts = handshake.text().split(":")
        if len(parts) != 4 or not parts[0] or "xhr-polling" not in parts[3].split(","):
            raise OverleafError("Overleaf did not provide a supported project metadata connection.")
        route = "/socket.io/1/xhr-polling/" + quote(parts[0], safe="")
        deadline = time.monotonic() + 30
        try:
            for _ in range(6):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                response = request.get(route, max_redirects=0, timeout=min(15_000, max(1, int(remaining * 1000))))
                if response.status != 200:
                    raise OverleafError(f"Unable to read upload folder metadata (HTTP {response.status}).")
                for frame in _frames(response.text()):
                    if frame.startswith(("0:", "7:")):
                        raise OverleafError("Overleaf rejected the project metadata connection. Check login and project access.")
                    if frame.startswith("2:"):
                        request.post(route, data="2::", headers={"Content-Type": "text/plain"},
                                     max_redirects=0, timeout=5_000)
                    if not frame.startswith("5:::"):
                        continue
                    try:
                        event = json.loads(frame[4:])
                    except ValueError:
                        raise OverleafError("Unexpected project metadata JSON.") from None
                    if not isinstance(event, dict):
                        raise OverleafError("Unexpected project metadata event.")
                    if event.get("name") == "connectionRejected":
                        raise OverleafError("Overleaf denied project metadata access. Check login and project access.")
                    if event.get("name") != "joinProjectResponse":
                        continue
                    args = event.get("args")
                    project = args[0].get("project") if isinstance(args, list) and args and isinstance(args[0], dict) else None
                    folders = project.get("rootFolder") if isinstance(project, dict) else None
                    if (not isinstance(project, dict) or project.get("_id") != project_id
                            or not isinstance(folders, list) or len(folders) != 1 or not isinstance(folders[0], dict)
                            or not isinstance(folders[0].get("_id"), str) or not folders[0]["_id"]):
                        raise OverleafError("Unexpected project root folder metadata.")
                    self._roots[project_id] = folders[0]["_id"]
                    return self._roots[project_id]
            raise OverleafError("Timed out reading the project root folder. Check Overleaf connectivity.")
        finally:
            try:
                request.post(route, data="0::", headers={"Content-Type": "text/plain"},
                             max_redirects=0, timeout=5_000)
            except Exception:
                # Closing the transient read connection must not hide the original result.
                pass

    @contextmanager
    def batch(self):
        """Share one authenticated HTTP request context across sequential uploads and always close it."""
        if self._request is not None:
            yield self
            return
        session = self.load()
        sync_playwright, request_error = _playwright()
        try:
            with sync_playwright() as playwright:
                request = playwright.request.new_context(
                    base_url=BASE_URL,
                    storage_state={"cookies": session["cookies"], "origins": []},
                    extra_http_headers={"X-CSRF-Token": session["csrf"], "Origin": BASE_URL,
                                        "Referer": BASE_URL + "/project", "Accept": "application/json"},
                    timeout=60_000,
                )
                self._request = request
                try:
                    yield self
                finally:
                    self._request = None
                    try:
                        request.dispose()
                    except request_error:
                        pass
        except request_error:
            raise OverleafError("Unable to upload the file. Check your connection and try again.") from None

    def upload(self, project_id, name, data):
        """Upload one source file through a shared context or an isolated standalone context."""
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or path.as_posix() != name or "\\" in name
                or any(part in {".", ".."} or ":" in part or "\x00" in part for part in name.split("/"))
                or any(part.startswith(".") for part in path.parts)):
            raise OverleafError("Cannot upload a hidden, unsafe, or reserved source path.")
        if self._request is None:
            with self.batch():
                return self.upload(project_id, name, data)
        _, request_error = _playwright()
        try:
            request = self._request
            folder_id = self._root(request, project_id)
            payload = {"name": path.name, "relativePath": name,
                       "qqfile": {"name": path.name, "mimeType": "application/octet-stream", "buffer": data}}
            for prefix in ("project", "Project"):
                response = request.post(f"/{prefix}/{quote(project_id, safe='')}/upload",
                                        params={"folder_id": folder_id}, max_redirects=0, multipart=payload)
                if response.status != 404:
                    break
            if response.status == 401 or 300 <= response.status < 400:
                raise LoginError("Session is no longer authenticated. Run leaf login again.")
            if response.status == 403:
                raise OverleafError("Overleaf denied the upload. Check write access or run leaf login again.")
            if response.status == 429:
                raise OverleafError("Overleaf rate limit reached. Try again later.")
            if response.status == 404:
                raise OverleafError("File upload failed (HTTP 404) on both supported upload routes. Check project access or API compatibility.")
            if response.status != 200:
                raise OverleafError(f"File upload failed (HTTP {response.status}).")
            try:
                result = response.json()
            except ValueError:
                raise OverleafError("Overleaf did not return upload JSON. Check login or API compatibility.") from None
            if not isinstance(result, dict) or result.get("success") is not True:
                raise OverleafError("Overleaf did not confirm the file upload. Check file limits or API compatibility.")
        except request_error:
            raise OverleafError("Unable to upload the file. Check your connection and try again.") from None

    def login(self, browser="chrome", timeout=300):
        """Open a new Chrome window by default, wait for login, and save session cookies."""
        if browser not in {"auto", "edge", "chrome", "chromium"}:
            raise LoginError("Browser must be auto, edge, chrome, or chromium.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise LoginError("Login timeout must be a positive number.")
        sync_playwright, browser_error = _playwright()
        channels = {"edge": "msedge", "chrome": "chrome", "chromium": "chromium"}
        candidates = ["chrome", "msedge", "chromium"] if browser == "auto" else [channels[browser]]
        try:
            with sync_playwright() as playwright, tempfile.TemporaryDirectory(prefix="leafbridge-login-") as profile:
                context = None
                for channel in candidates:
                    try:
                        options = {"channel": channel, "headless": False, "timeout": 30_000}
                        executable = _browser_path(channel)
                        if executable:
                            options["executable_path"] = executable
                        context = playwright.chromium.launch_persistent_context(profile, **options)
                        break
                    except browser_error:
                        continue
                if context is None:
                    raise LoginError("Unable to launch the browser. Check that the selected browser is installed, or run python -m playwright install chromium and use --browser chromium.")
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    response = page.goto(BASE_URL + "/login", wait_until="domcontentloaded", timeout=30_000)
                    if response is not None and response.status >= 500:
                        raise LoginError("Overleaf is temporarily unavailable. Try again later.")
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        pages = context.pages
                        if not pages:
                            raise LoginError("The login window was closed. Session was not saved.")
                        for current_page in pages:
                            location = urlsplit(current_page.url)
                            if (location.scheme != "https" or location.netloc != "www.overleaf.com"
                                    or not (location.path == "/project" or location.path.startswith("/project/"))):
                                continue
                            try:
                                token = current_page.locator('meta[name="ol-csrfToken"]').first.get_attribute("content", timeout=500)
                                cookies = context.cookies(BASE_URL + "/project")
                                data = _validate({"version": 1, "base_url": BASE_URL,
                                                          "csrf": token, "cookies": cookies})
                            except (browser_error, LoginError):
                                continue
                            return self._save(data)
                        pages[0].wait_for_timeout(500)
                    raise LoginError("Login timed out. Session was not saved. Run leaf login again or increase --timeout.")
                finally:
                    try:
                        context.close()
                    except browser_error:
                        pass
        except browser_error:
            raise LoginError("The browser was closed or could not access Overleaf. Session was not saved. Check your connection and try again.") from None
        except OSError:
            raise LoginError("Unable to create or clean up the temporary browser directory. Check directory permissions.") from None
