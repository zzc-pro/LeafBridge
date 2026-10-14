"""Compare committed and working-tree push speed against a loopback-only HTTP server."""

import argparse
from collections import Counter
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import zipfile

from leafbridge import client, git, sync


def archive(files):
    """Build a deterministic fake remote ZIP from source bytes."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for name, content in files.items():
            stream.writestr(name, content)
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        """Disable packet coalescing to avoid artificial loopback response delays."""
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *args):
        """Suppress per-request server logging during timing."""
        pass

    def send(self, data, kind="text/plain", status=200):
        """Return a bounded HTTP response with an explicit length."""
        data = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """Serve only fake ZIP and project-root metadata endpoints."""
        path = urlsplit(self.path).path
        state = self.server.state
        if path == "/Project/bench-project/download/zip":
            self.send(state["zip"], "application/zip")
        elif path == "/socket.io/1/":
            state["roots"] += 1
            self.send("bench-session:60:60:websocket,xhr-polling")
        elif path == "/socket.io/1/xhr-polling/bench-session":
            event = "5:::" + json.dumps({"name": "joinProjectResponse", "args": [{"project": {
                "_id": "bench-project", "rootFolder": [{"_id": "bench-root"}]}}]})
            frames = ["1::", event]
            self.send("".join("\ufffd" + str(len(frame)) + "\ufffd" + frame for frame in frames))
        else:
            self.send("Not found", status=404)

    def do_POST(self):
        """Validate actual multipart file uploads and retain their bytes in memory."""
        url = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if url.path == "/socket.io/1/xhr-polling/bench-session":
            self.send("1")
            return
        if url.path != "/project/bench-project/upload" or parse_qs(url.query).get("folder_id") != ["bench-root"]:
            self.send("Not found", status=404)
            return
        header = ("Content-Type: " + self.headers.get("Content-Type", "") + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
        message = BytesParser(policy=default).parsebytes(header + body)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                  for part in message.iter_parts()}
        name = fields["relativePath"].decode()
        self.server.state["files"][name] = fields["qqfile"]
        self.server.state["uploads"] += 1
        self.send('{"success": true}', "application/json")


def before(revision):
    """Load the baseline client, Git helpers, and synchronization code directly from Git."""
    package = types.ModuleType("leafbridge_before")
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name in ["client", "git", "sync"]:
        source = subprocess.run([git.check(), "show", f"{revision}:src/leafbridge/{name}.py"],
                                check=True, capture_output=True, timeout=30).stdout.decode("utf-8")
        module = types.ModuleType(package.__name__ + "." + name)
        module.__package__ = package.__name__
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(source, f"<baseline/{name}.py>", "exec"), module.__dict__)
    return package


def measure(server, module, engine, count, unchanged=False):
    """Time a full push with real HTTP requests, Git selection, hashing, and per-file state saves."""
    size = 8192
    names = [f"sections/paper_{number:03d}.tex" for number in range(count)]
    original = {name: b"old blue leaf\n".ljust(size, b"o") for name in names}
    local = original if unchanged else {name: b"new red bicycle\n".ljust(size, b"n") for name in names}
    counters = Counter()
    factory, error = module._playwright()

    @contextmanager
    def start():
        """Count actual Playwright runtime and HTTP context creation without introducing delays."""
        counters["runtimes"] += 1
        with factory() as playwright:
            create = playwright.request.new_context

            def context(**kwargs):
                """Count and create an actual Playwright HTTP request context."""
                counters["contexts"] += 1
                return create(**kwargs)

            with patch.object(playwright.request, "new_context", side_effect=context):
                yield playwright

    def load():
        """Use fake credentials only; never read the user's saved login session."""
        counters["loads"] += 1
        return {"cookies": [], "csrf": "bench-csrf"}

    with tempfile.TemporaryDirectory(prefix="leafbridge-speed-") as temporary:
        root = Path(temporary).resolve()
        git.init(root)
        metadata = root / ".leafbridge"
        metadata.mkdir()
        base = {name: hashlib.sha256(content).hexdigest() for name, content in original.items()}
        (metadata / "state.json").write_text(json.dumps({"version": 1, "files": base}), encoding="utf-8")
        for name, content in local.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        server.state = {"zip": archive(original), "files": dict(original), "uploads": 0, "roots": 0}
        api = module.OverleafClient(session_path=metadata / "unused-session.json")
        api.load = load
        address = f"http://127.0.0.1:{server.server_port}"
        with patch.object(module, "BASE_URL", address), patch.object(module, "_playwright", return_value=(start, error)):
            began = time.perf_counter()
            result = engine.run(api, root, "bench-project", base, "push")
            elapsed = time.perf_counter() - began
        expected = 0 if unchanged else count
        assert result == (expected, {}), result
        assert server.state["files"] == local
        assert server.state["uploads"] == expected
        assert server.state["roots"] == (1 if expected else 0)
        saved = json.loads((metadata / "state.json").read_text(encoding="utf-8"))["files"]
        assert saved == {name: hashlib.sha256(content).hexdigest() for name, content in local.items()}
        assert getattr(api, "_request", None) is None
        return {"seconds": elapsed, "uploads": expected, "root_reads": server.state["roots"], **counters}


def main():
    """Run paired trials and save raw timings plus median summaries for reproducible comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD", help="Commit to compare with the current working tree.")
    parser.add_argument("--repeats", type=int, default=3, help="Paired trials per workload (default: 3).")
    parser.add_argument("--output", default=".cache/push-speed.json", help="JSON output path.")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    revision = subprocess.run([git.check(), "rev-parse", "--verify", "--end-of-options", args.baseline + "^{commit}"],
                              check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    baseline = before(revision)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw, summaries = [], []
    try:
        # Warm both versions before collecting timings.
        for module, engine in [(baseline.client, baseline.sync), (client, sync)]:
            measure(server, module, engine, 1)
        for name, count, unchanged in [("unchanged", 10, True), ("one", 1, False),
                                        ("ten", 10, False), ("thirty", 30, False)]:
            trials = {"before": [], "after": []}
            for repeat in range(args.repeats):
                order = ["before", "after"] if repeat % 2 == 0 else ["after", "before"]
                for variant in order:
                    module, engine = (baseline.client, baseline.sync) if variant == "before" else (client, sync)
                    print(f"Running {name}: {variant}, trial {repeat + 1}/{args.repeats}", flush=True)
                    result = measure(server, module, engine, count, unchanged)
                    trials[variant].append(result)
                    raw.append({"workload": name, "trial": repeat + 1, "variant": variant, **result})
                    print(f"  {result['seconds']:.3f}s; runtimes: {result['runtimes']}; uploads: {result['uploads']}", flush=True)
            old = statistics.median(item["seconds"] for item in trials["before"])
            new = statistics.median(item["seconds"] for item in trials["after"])
            summaries.append({"workload": name, "files": count, "changed": 0 if unchanged else count,
                              "before_seconds": old, "after_seconds": new, "speedup": old / new})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"baseline": revision, "repeats": args.repeats, "bytes_per_file": 8192,
                                 "environment": "loopback HTTP, real Playwright, no injected latency",
                                 "summary": summaries, "trials": raw}, indent=2) + "\n", encoding="utf-8")
    for row in summaries:
        print(f"{row['workload']}: {row['before_seconds']:.3f}s -> {row['after_seconds']:.3f}s ({row['speedup']:.2f}x)")
    print(f"Results saved to: {output}")


if __name__ == "__main__":
    main()
