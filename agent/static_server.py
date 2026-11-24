"""Loopback-only preview of public static assets in detected frontend projects."""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class StaticAssets(SimpleHTTPRequestHandler):
    extensions = {".html", ".htm", ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif",
                  ".svg", ".ico", ".webp", ".avif", ".woff", ".woff2", ".ttf", ".mp4", ".webm"}

    def send_head(self):
        root = Path(self.directory).resolve()
        path = Path(self.translate_path(self.path)).resolve()
        if path.is_dir():
            path = (path / "index.html").resolve()
        if (not path.is_relative_to(root) or path.suffix.lower() not in self.extensions
                or any(part.startswith(".") for part in path.relative_to(root).parts)):
            self.send_error(404)
            return None
        if not path.is_file():
            self.send_error(404)
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None


if __name__ == "__main__":
    handler = partial(StaticAssets, directory=sys.argv[1])
    with ThreadingHTTPServer(("127.0.0.1", 3000), handler) as server:
        print("Serving at http://127.0.0.1:3000", flush=True)
        server.serve_forever()
