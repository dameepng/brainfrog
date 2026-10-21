# -*- coding: utf-8 -*-
"""BrainFrog-controlled static application launcher (runs inside the app container).

Standard library only. Mounted read-only at /bf-harness in the application container
for the ``python-static`` profile. It:

1. Recomputes the digest of the read-only source snapshot mounted at --source using the
   same algorithm as core.runtime.sandbox.compute_directory_digest, and emits it as a
   nonce-bound record on stdout (observed by BrainFrog through the container log).
2. Serves the snapshot as static files: GET/HEAD only, no directory listings, no
   symlinks, no paths outside the snapshot. Clean URLs map '/login' to 'login.html'.

No repository code is imported or executed server-side.
"""

import hashlib
import json
import mimetypes
import os
import posixpath
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

RECORD_PREFIX = "@@BF_APP@@ "
MAX_LOG_LINES = 2000


def _emit(event, nonce, **payload):
    record = {"event": event, "nonce": nonce}
    record.update(payload)
    sys.stdout.write("\n" + RECORD_PREFIX + json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


def _directory_digest(source):
    entries = []
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs.sort()
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            entries.append((os.path.relpath(full, source).replace(os.sep, "/"), full))
    entries.sort(key=lambda e: e[0])
    hasher = hashlib.sha256()
    for rel, full in entries:
        with open(full, "rb") as fh:
            data = fh.read()
        hasher.update(f"{rel}:{len(data)}:".encode("utf-8"))
        hasher.update(hashlib.sha256(data).digest())
    return hasher.hexdigest(), len(entries)


def _make_handler(source_root):
    root_real = os.path.realpath(source_root)
    log_count = {"n": 0}

    class StaticHandler(BaseHTTPRequestHandler):
        server_version = "BrainFrogStatic/1"
        sys_version = ""

        def log_message(self, fmt, *args):
            if log_count["n"] < MAX_LOG_LINES:
                log_count["n"] += 1
                sys.stderr.write("app-request %s\n" % (fmt % args))

        def _resolve(self):
            path = unquote(urlsplit(self.path).path)
            if "\x00" in path or "\\" in path:
                return None
            norm = posixpath.normpath(path)
            parts = [p for p in norm.split("/") if p]
            if any(p in ("..", ".") for p in parts):
                return None
            candidates = []
            rel = "/".join(parts)
            if not rel:
                candidates.append("index.html")
            else:
                candidates.extend([rel, rel + ".html", rel + "/index.html"])
            for cand in candidates:
                full = os.path.join(source_root, *cand.split("/"))
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                real = os.path.realpath(full)
                if os.path.commonpath([real, root_real]) != root_real:
                    continue
                return full
            return None

        def _serve(self, include_body):
            full = self._resolve()
            if full is None:
                body = b"Not Found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if include_body:
                    self.wfile.write(body)
                return
            with open(full, "rb") as fh:
                data = fh.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def do_GET(self):
            self._serve(True)

        def do_HEAD(self):
            self._serve(False)

        def _method_not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    return StaticHandler


def main(argv):
    args = {"--source": None, "--port": None, "--nonce-file": None}
    it = iter(argv)
    for tok in it:
        if tok not in args:
            sys.stderr.write(f"app_launcher: unknown argument {tok!r}\n")
            return 2
        args[tok] = next(it, None)
    if None in args.values():
        sys.stderr.write("app_launcher: --source, --port and --nonce-file are required\n")
        return 2
    try:
        with open(args["--nonce-file"], "r", encoding="ascii") as fh:
            nonce = fh.read().strip()
        port = int(args["--port"])
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"app_launcher: invalid launch inputs: {exc}\n")
        return 2
    source = args["--source"]
    if not os.path.isdir(source):
        sys.stderr.write("app_launcher: source directory missing\n")
        return 2

    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    digest, count = _directory_digest(source)
    _emit("source_digest", nonce, digest=digest, file_count=count)

    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(source))
    server.daemon_threads = True
    _emit("listening", nonce, port=port)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
