"""Local-only mock bridge preview; never part of AstrBot's production API."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
VIEW = ROOT / "views" / "console"
TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in {"/", "/preview", "/preview/"}:
            body = (VIEW / "index.html").read_text(encoding="utf-8")
            body = body.replace(
                '<script type="module"',
                '<script src="/tests/mock-bridge.js"></script><script type="module"',
            )
            self.respond(body.encode("utf-8"), "text/html")
            return
        if path.startswith("/preview/"):
            target = VIEW / path.removeprefix("/preview/")
            allowed = VIEW
        elif path == "/tests/mock-bridge.js":
            target = ROOT / "tests" / "mock-bridge.js"
            allowed = ROOT / "tests"
        elif path == "/tests/en-US.json":
            target = ROOT / ".astrbot-plugin" / "i18n" / "en-US.json"
            allowed = ROOT / ".astrbot-plugin" / "i18n"
        else:
            self.send_error(404)
            return
        try:
            target.resolve().relative_to(allowed.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        self.respond(target.read_bytes(), TYPES.get(target.suffix, "text/plain"))

    def respond(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8814)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
