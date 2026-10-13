"""Serve the built site plus a recorded slice of PyPI, for the sandbox e2e test.

    python tools/web_mirror.py record MIRROR_DIR REQ [REQ ...] [--python 3.12]
    python tools/web_mirror.py serve MIRROR_DIR [--port 8765]

``record`` runs pyxbuild's built-in resolver against PyPI and stores every
response it needed (JSON simple pages with file URLs rewritten to
``/files/...``, ``.metadata`` files) plus the chosen wheels. ``serve`` serves
build/site at ``/`` and the recording at ``/simple/`` and ``/files/``, so a
headless browser can build without reaching the internet (and the test does
not depend on what PyPI publishes tomorrow).
"""

from __future__ import annotations

import argparse
import http.server
import json
import sys
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def record(mirror: Path, requirements: list[str], python: str) -> None:
    from pyxbuild.download import http_get
    from pyxbuild.resolve import resolve
    from pyxbuild.runtime import runtime_spec
    from pyxbuild.targets import WIN_AMD64

    full = runtime_spec(WIN_AMD64, python).version
    meta_urls: dict[str, str] = {}

    def get(url: str, headers: dict[str, str]) -> bytes:
        data = http_get(url, headers)
        if url.endswith(".metadata"):
            name = url.rsplit("/", 1)[1]
            out = mirror / "files" / name
        else:
            page = json.loads(data)
            for f in page.get("files", []):
                meta_urls[f["filename"]] = f["url"]
                f["url"] = f"/files/{f['filename']}"
            data = json.dumps(page).encode()
            out = mirror / "simple" / url.rstrip("/").rsplit("/", 1)[1] / "index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return data

    def real(url: str, headers: dict[str, str]) -> bytes:  # resolver sees local URLs
        if url.startswith("/files/") or "/files/" in url:
            name = url.rsplit("/", 1)[1]
            base = name[: -len(".metadata")] if name.endswith(".metadata") else name
            return get(meta_urls[base] + (".metadata" if name.endswith(".metadata") else ""),
                       headers)
        return get(url, headers)

    pins = resolve(requirements, WIN_AMD64.tags(python), WIN_AMD64.marker_environment(full),
                   full, get=real, index_url="https://pypi.org/simple")
    for pin in pins:
        for w in pin.files:
            out = mirror / "files" / w.filename
            if not out.exists():
                out.write_bytes(http_get(meta_urls[w.filename]))
    print("recorded:", ", ".join(f"{p.name} {p.version}" for p in pins))


class _Handler(http.server.SimpleHTTPRequestHandler):
    mirror: Path

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0]
        if clean.startswith("/simple/"):
            return str(self.mirror / clean.lstrip("/").rstrip("/") / "index.json")
        if clean.startswith("/files/"):
            return str(self.mirror / clean.lstrip("/"))
        return super().translate_path(path)

    def end_headers(self) -> None:
        if self.path.startswith("/simple/"):
            self.send_header("Content-Type", "application/vnd.pypi.simple.v1+json")
        super().end_headers()

    def guess_type(self, path):  # noqa: ANN001
        if str(path).endswith(".wasm"):
            return "application/wasm"
        return super().guess_type(path)

    def log_message(self, *args) -> None:  # quiet
        return None


def serve(mirror: Path, port: int) -> None:
    handler = partial(type("H", (_Handler,), {"mirror": mirror}),
                      directory=str(ROOT / "build" / "site"))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving on http://127.0.0.1:{port}/", flush=True)
        httpd.serve_forever()


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("mirror", type=Path)
    r.add_argument("requirements", nargs="+")
    r.add_argument("--python", default="3.12")
    s = sub.add_parser("serve")
    s.add_argument("mirror", type=Path)
    s.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    if a.cmd == "record":
        record(a.mirror, a.requirements, a.python)
    else:
        serve(a.mirror, a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
