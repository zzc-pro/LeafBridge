"""Overleaf 浏览器登录和会话保存。"""

import json
import math
import os
import shutil
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit

BASE_URL = "https://www.overleaf.com"
SESSION_COOKIES = {"overleaf_session2", "sharelatex.sid"}


class LoginError(RuntimeError):
    """可直接展示给用户的登录错误。"""


def default_session_path():
    """会话存放在用户配置目录，与论文 Git 仓库分开。"""
    if sys.platform == "win32":
        directory = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support"
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return directory / "leafbridge" / "session.json"


def _validate_session(data):
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


def _load_playwright():
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise LoginError("Login dependency is missing. Run python -m pip install -e . in the leafbridge Conda environment.") from None
    return sync_playwright, Error


def _browser_executable(channel):
    """优先定位已安装的浏览器，避免只依赖 Playwright 的默认安装路径。"""
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
        self.session_path = Path(session_path) if session_path is not None else default_session_path()

    def load_session(self):
        """读取并检查会话；服务器仍可能提前撤销未到期的 Cookie。"""
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LoginError("Not logged in. Run leaf login.") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise LoginError("Unable to read the session. Run leaf login again.") from None
        return _validate_session(data)

    def _save_session(self, data):
        data = _validate_session(data)
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

    def login(self, browser="chrome", timeout=300):
        """默认新开独立 Chrome 窗口，等待用户登录并保存会话 Cookie。"""
        if browser not in {"auto", "edge", "chrome", "chromium"}:
            raise LoginError("Browser must be auto, edge, chrome, or chromium.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise LoginError("Login timeout must be a positive number.")
        sync_playwright, browser_error = _load_playwright()
        channels = {"edge": "msedge", "chrome": "chrome", "chromium": "chromium"}
        candidates = ["chrome", "msedge", "chromium"] if browser == "auto" else [channels[browser]]
        try:
            with sync_playwright() as playwright, tempfile.TemporaryDirectory(prefix="leafbridge-login-") as profile:
                context = None
                for channel in candidates:
                    try:
                        options = {"channel": channel, "headless": False, "timeout": 30_000}
                        executable = _browser_executable(channel)
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
                                data = _validate_session({"version": 1, "base_url": BASE_URL,
                                                          "csrf": token, "cookies": cookies})
                            except (browser_error, LoginError):
                                continue
                            return self._save_session(data)
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
