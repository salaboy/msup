#!/usr/bin/env python3
"""Build the site and serve it locally.

Serves at the configured base_path (default /msup/) so local preview matches
the real GitHub Pages URL structure, including the 404 behaviour.

    python3 scripts/serve.py            # http://localhost:8000/msup/
    python3 scripts/serve.py --port 9000
"""

import argparse
import functools
import http.server
import json
import os
import socketserver
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build as builder  # noqa: E402


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the build under base_path and uses the real 404.html."""

    base_path = "/"

    def translate_path(self, path):
        if self.base_path != "/" and path.startswith(self.base_path.rstrip("/")):
            path = path[len(self.base_path.rstrip("/")):] or "/"
        return super().translate_path(path)

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            page = os.path.join(self.directory, "404.html")
            if os.path.exists(page):
                with open(page, "rb") as fh:
                    body = fh.read()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
        super().send_error(code, message, explain)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-build", action="store_true", help="serve the existing _site/")
    args = ap.parse_args()

    out = os.path.join(ROOT, "_site")
    if not args.no_build:
        if builder.main(["--out", out]) != 0:
            return 1

    with open(os.path.join(ROOT, "data", "site.json"), encoding="utf-8") as fh:
        base_path = json.load(fh)["base_path"].rstrip("/") + "/"

    Handler.base_path = base_path
    handler = functools.partial(Handler, directory=out)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", args.port), handler) as httpd:
        url = "http://localhost:%d%s" % (args.port, base_path)
        print("\nserving %s\n  %s\n  ctrl-c to stop\n" % (os.path.relpath(out, ROOT), url))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
