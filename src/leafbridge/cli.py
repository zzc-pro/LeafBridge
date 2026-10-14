"""Command-line entry point for LeafBridge."""

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile

from . import git, sync
from .client import BASE_URL, OverleafError, OverleafClient
from .sync import clone


def _timeout(value):
    """Parse a finite positive timeout value from command-line input."""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive number.")
    return seconds


def add(api, name=None, project_id=None, path=None):
    """Associate an existing working tree with one active project without transferring files."""
    if (name is None) == (project_id is None) or not (name or project_id):
        raise OverleafError("Specify an exact project name or --id, but not both.")
    root = git.root(path)
    metadata = root / ".leafbridge"
    if metadata.is_symlink() or metadata.resolve().parent != root:
        raise OverleafError("The repository .leafbridge must not be a symbolic link or point outside the repository.")
    previous = None
    try:
        if metadata.exists():
            config = metadata / "config.json"
            if not metadata.is_dir() or config.is_symlink() or not config.is_file():
                raise OverleafError("Existing LeafBridge metadata is incomplete or unsafe. Check .leafbridge.")
            previous = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or not previous.get("project_id"):
                raise OverleafError("Existing LeafBridge configuration is invalid. Check .leafbridge/config.json.")
        key, value = ("id", project_id) if project_id is not None else ("name", name)
        matches = [project for project in api.projects() if project[key] == value]
        if not matches:
            raise OverleafError(f"No active project with {key} {value!r}. Run leaf list to check projects.")
        if len(matches) != 1:
            raise OverleafError("Multiple active projects match. Use leaf remote add --id PROJECT_ID.")
        project = matches[0]
        if previous is not None:
            if previous.get("base_url") == BASE_URL and previous["project_id"] == project["id"]:
                return root, project, False
            raise OverleafError("This repository is already associated with another project. Existing configuration was preserved.")
        with tempfile.TemporaryDirectory(prefix=".leafbridge-add-", dir=root) as temporary:
            staged = Path(temporary) / "metadata"
            staged.mkdir()
            config = {"base_url": BASE_URL, "project_id": project["id"], "name": project["name"]}
            for filename, payload in [("config.json", config), ("state.json", {"version": 1, "files": {}})]:
                (staged / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
            git.ignore(root)
            if metadata.exists() or metadata.is_symlink():
                raise OverleafError("LeafBridge metadata appeared during setup. Try again after checking .leafbridge.")
            staged.rename(metadata)
    except (OSError, ValueError, UnicodeError):
        raise OverleafError("Unable to associate the project. Check LeafBridge configuration and repository permissions.") from None
    return root, project, True


def _read(root, filename):
    """Read repository metadata only from regular files inside the metadata directory."""
    try:
        metadata = root / ".leafbridge"
        target = metadata / filename
        if (metadata.is_symlink() or metadata.resolve().parent != root
                or target.is_symlink() or target.resolve().parent != metadata.resolve()):
            raise OverleafError("LeafBridge metadata contains an unsafe path.")
        if not target.is_file():
            raise OverleafError(f"Missing .leafbridge/{filename}. Run leaf remote add or leaf clone first.")
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError, UnicodeError):
        raise OverleafError(f"Unable to read .leafbridge/{filename}. Check its format and permissions.") from None


def _load(path=None):
    """Load and validate the working tree association and synchronization state."""
    root = git.root(path)
    config = _read(root, "config.json")
    state = _read(root, "state.json")
    if (config.get("base_url") != BASE_URL or not isinstance(config.get("project_id"), str)
            or not config["project_id"].strip()
            or ("name" in config and not isinstance(config["name"], str))):
        raise OverleafError("Invalid project configuration. Check .leafbridge/config.json.")
    if type(state.get("version")) is not int or state["version"] != 1 or "files" not in state:
        raise OverleafError("Unsupported synchronization baseline. Check .leafbridge/state.json.")
    return root, config, state["files"]


def status(api, path=None):
    """Load the association and baseline, then inspect current local and remote contents."""
    root, config, files = _load(path)
    rows = sync.status(api, root, config["project_id"], files)
    return root, config, rows


def transfer(api, direction, force=False, path=None):
    """Run a manual push or pull and print completed files and skipped differences."""
    root, config, files = _load(path)
    count, skipped = sync.run(api, root, config["project_id"], files, direction, force,
                              report=lambda action, name: print(f"{action}: {json.dumps(name, ensure_ascii=False)}", flush=True))
    for name, label in skipped.items():
        print(f"Skipped ({label}): {json.dumps(name, ensure_ascii=False)}")
    print(f"{direction.capitalize()} complete. Transferred: {count}; skipped differences: {len(skipped)}.")
    if skipped:
        print("Skipped files were preserved. Source deletions are not propagated; other changes may need the opposite direction.")
    return 0


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
    commands.add_parser("status", help="Compare local and remote files without modifying files or synchronization state.")
    for direction in ["push", "pull"]:
        command = commands.add_parser(direction, help="Upload local changes to Overleaf." if direction == "push"
                                      else "Download Overleaf changes to the local repository.")
        command.add_argument("--force", action="store_true", help="Prefer source contents for differing files; never propagate deletions.")
    clone_command = commands.add_parser("clone", help="Clone an active Overleaf project into a local Git repository.")
    clone_command.add_argument("name", help="Exact project name; always wrap it in double quotes.")
    clone_command.add_argument("directory", nargs="?", help="Local directory (default: the project name).")
    remote = commands.add_parser("remote", help="Associate a local repository with Overleaf.")
    actions = remote.add_subparsers(dest="action", required=True)
    add_command = actions.add_parser("add", help="Associate this Git working tree with an active project.")
    add_command.add_argument("name", nargs="?", help="Exact project name, wrapped in double quotes.")
    add_command.add_argument("--id", dest="project_id", help="Project ID from leaf list, instead of a name.")
    args = parser.parse_args(argv)
    if args.command == "remote" and ((args.name is None) == (args.project_id is None)
                                      or not (args.name or args.project_id)):
        add_command.error("specify an exact project name or --id, but not both")
    operation = {"login": "Login", "list": "Listing projects", "clone": "Clone", "remote": "Remote add",
                 "status": "Status", "push": "Push", "pull": "Pull"}[args.command]
    try:
        if args.command in {"push", "pull"}:
            return transfer(OverleafClient(), args.command, args.force)
        if args.command == "status":
            root, config, rows = status(OverleafClient())
            name = " ".join(config.get("name", config["project_id"]).split())
            print(f"Project: {name} ({config['project_id']})")
            print(f"Local repository: {root}")
            changes = {name: value for name, value in rows.items() if value != "same"}
            if changes:
                print(f"{'STATUS':<16}  FILE")
                for name, value in changes.items():
                    print(f"{value:<16}  {json.dumps(name, ensure_ascii=False)}")
            else:
                print("No file differences found.")
            conflicts = sum(value == "conflict" for value in rows.values())
            print(f"Files: {len(rows)}; same: {len(rows) - len(changes)}; differences: {len(changes)}; conflicts: {conflicts}")
            print("Inspection only. No files or synchronization state were changed; deletions are not propagated.")
            return 0
        if args.command == "remote":
            path, project, created = add(OverleafClient(), args.name, args.project_id)
            message = "Project associated" if created else "Project already associated"
            print(f"{message}: {project['name']!r} ({project['id']}) at {path}")
            return 0
        if args.command == "clone":
            print(f"Cloning {args.name!r}...", flush=True)
            path = clone(OverleafClient(), args.name, args.directory)
            print(f"Project cloned to: {path}")
            return 0
        if args.command == "list":
            projects = OverleafClient().projects()
            if not projects:
                print("No projects found.")
                return 0
            print(f"{'ID':<24}  {'STATUS':<8}  NAME")
            for project in projects:
                label = "trashed" if project.get("trashed") else "archived" if project.get("archived") else "active"
                name = " ".join(project["name"].split())
                print(f"{project['id']:<24}  {label:<8}  {name}")
            print(f"Projects: {len(projects)}")
            return 0
        print("Opening Overleaf in a new browser window. Complete login on the website, including CAPTCHA or SSO.", flush=True)
        session_path = OverleafClient().login(browser=args.browser, timeout=args.timeout)
    except OverleafError as error:
        print(f"{operation} failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\n{operation} cancelled.", file=sys.stderr)
        return 130
    print(f"Login successful. Session saved to: {session_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
