"""LeafBridge 命令行入口。"""

import argparse
import math
import sys

from .client import LoginError, OverleafClient


def _positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(prog="leaf", description="Bridge local Git repositories and Overleaf.")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Open a browser, log in to Overleaf, and save the session.")
    login.add_argument("--browser", choices=["auto", "edge", "chrome", "chromium"], default="chrome",
                       help="Choose a browser. Default: a new Chrome window. auto tries Chrome, Edge, then Chromium.")
    login.add_argument("--timeout", type=_positive_seconds, default=300,
                       help="Seconds to wait for login (default: 300).")
    args = parser.parse_args(argv)
    try:
        print("Opening Overleaf in a new browser window. Complete login on the website, including CAPTCHA or SSO.", flush=True)
        session_path = OverleafClient().login(browser=args.browser, timeout=args.timeout)
    except LoginError as error:
        print(f"Login failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130
    print(f"Login successful. Session saved to: {session_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
