"""Build the documentation site with the in-browser sandbox (GitHub Pages).

    python tools/build_site.py [--runtimes 3.12,3.13 | --no-runtimes] [--serve]

1. build/site-src: docs/ plus README.md as index.md (links rewritten);
2. mkdocs build (Material theme) -> build/site;
3. the sandbox's files next to docs/assets/try/worker.js:
   pyodide/  the Pyodide distribution from npm (pinned below);
   wheels/   pyxbuild (built from this checkout) and its pure-Python
             dependencies, plus wheels.json;
   runtimes/ the pinned python-build-standalone archives at the site root:
             GitHub release downloads do not allow cross-origin requests,
             files on the same site do.

Requires: pip install mkdocs-material, network access.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "build" / "site-src"
SITE = ROOT / "build" / "site"
PYODIDE = "0.28.3"  # Python 3.13
PURE_DEPS = ["packaging>=25", "installer>=1.0.1", "resolvelib>=1.1"]


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pyxbuild-site"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def prepare_sources() -> None:
    if SRC.exists():
        shutil.rmtree(SRC)
    shutil.copytree(ROOT / "docs", SRC)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = re.sub(r"\]\(docs/", "](", readme)
    readme = readme.replace("](LICENSE)", "](https://github.com/sedrew/pyxbuild/blob/main/LICENSE)")
    (SRC / "index.md").write_text(readme, encoding="utf-8")


def mkdocs_build() -> None:
    subprocess.run([sys.executable, "-m", "mkdocs", "build", "--clean", "-f",
                    str(ROOT / "mkdocs.yml")], check=True, cwd=ROOT)


def add_pyodide(dest: Path) -> None:
    meta = json.loads(_get(f"https://registry.npmjs.org/pyodide/{PYODIDE}"))
    data = _get(meta["dist"]["tarball"])
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile() or not member.name.startswith("package/"):
                continue
            name = member.name[len("package/"):]
            if "/" in name or name.endswith((".d.ts", ".map", ".html", ".md")):
                continue
            (dest / name).write_bytes(tf.extractfile(member).read())


def add_wheels(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(dest),
                    str(ROOT)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--no-deps",
                    "--only-binary", ":all:", "-d", str(dest), *PURE_DEPS], check=True)
    names = sorted(p.name for p in dest.glob("*.whl"))
    (dest / "wheels.json").write_text(json.dumps(names, indent=1) + "\n", encoding="utf-8")


def add_runtimes(dest: Path, only: set[str] | None) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pyxbuild.download import fetch
    from pyxbuild.runtime import _table, runtime_spec
    from pyxbuild.targets import WIN_AMD64

    table = _table()
    for python in sorted(table["runtimes"]["win_amd64"]):
        if only is not None and python not in only:
            continue
        spec = runtime_spec(WIN_AMD64, python)
        path = fetch(spec.url, spec.sha256, spec.file)
        out = dest / spec.release / spec.file
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
        print(f"  runtime {python}: {out.relative_to(SITE)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runtimes", help="comma-separated Python versions (default: all pinned)")
    p.add_argument("--no-runtimes", action="store_true")
    p.add_argument("--serve", action="store_true", help="serve build/site on :8000 afterwards")
    args = p.parse_args(argv)
    prepare_sources()
    mkdocs_build()
    try_dir = SITE / "assets" / "try"
    add_pyodide(try_dir / "pyodide")
    add_wheels(try_dir / "wheels")
    if not args.no_runtimes:
        only = set(args.runtimes.split(",")) if args.runtimes else None
        add_runtimes(SITE / "runtimes", only)
    (SITE / ".nojekyll").write_text("")
    print(f"site: {SITE}")
    if args.serve:
        subprocess.run([sys.executable, "-m", "http.server", "8000", "-d", str(SITE)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
