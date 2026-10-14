"""Command-line entry point for LeafBridge."""

import argparse
import math
import sys

from .client import OverleafError, OverleafClient


def _timeout(value):
    """Parse a finite positive timeout value from command-line input."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.")
    return seconds


def main(argv=None):
    """Parse CLI arguments, run the requested operation, and return its exit code."""
    parser = argparse.ArgumentParser(prog="leaf", description="Bridge local Git repositories and Overleaf.")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Open a browser, log in to Overleaf, and save the session.")
    login.add_argument("--browser", choices=["auto", "edge", "chrome", "chromium"], default="chrome",
                       help="Choose a browser. Default: a new Chrome window. auto tries Chrome, Edge, then Chromium.")
    login.add_argument("--timeout", type=_timeout, default=300,
                       help="Seconds to wait for login (default: 300).")
    commands.add_parser("list", help="List active account projects, excluding archived and trashed projects.")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            projects = OverleafClient().projects()
            if not projects:
                print("No projects found.")
                return 0
            print(f"{'ID':<24}  {'STATUS':<8}  NAME")
            for project in projects:
                status = "trashed" if project.get("trashed") else "archived" if project.get("archived") else "active"
                name = " ".join(project["name"].split())
                print(f"{project['id']:<24}  {status:<8}  {name}")
            print(f"Projects: {len(projects)}")
            return 0
        print("Opening Overleaf in a new browser window. Complete login on the website, including CAPTCHA or SSO.", flush=True)
        session_path = OverleafClient().login(browser=args.browser, timeout=args.timeout)
    except OverleafError as error:
        operation = "Login" if args.command == "login" else "Listing projects"
        print(f"{operation} failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        operation = "Login" if args.command == "login" else "Listing projects"
        print(f"\n{operation} cancelled.", file=sys.stderr)
        return 130
    print(f"Login successful. Session saved to: {session_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
