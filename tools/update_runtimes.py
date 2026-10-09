"""Pin a python-build-standalone release in src/pyxbuild/runtimes.json.

Usage::

    python tools/update_runtimes.py 20260718

Reads the release's SHA256SUMS and records, for every supported target and
Python minor version, the newest ``install_only_stripped`` archive with its
hash. Pinning (instead of asking for "latest" at build time) keeps builds
reproducible; updating the pin is a reviewed commit.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyxbuild.targets import SUPPORTED_PYTHONS, TARGETS  # noqa: E402

BASE = "https://github.com/astral-sh/python-build-standalone/releases/download"
FLAVOR = "install_only_stripped"


def main() -> int:
    release = sys.argv[1]
    with urllib.request.urlopen(f"{BASE}/{release}/SHA256SUMS", timeout=60) as resp:
        sums = resp.read().decode()
    runtimes: dict[str, dict[str, dict[str, str]]] = {}
    for target in TARGETS.values():
        found: dict[str, dict[str, str]] = {}
        pattern = re.compile(
            rf"^([0-9a-f]{{64}})\s+(cpython-(\d+\.\d+)\.(\d+)\+{release}-"
            rf"{re.escape(target.pbs_triple)}-{FLAVOR}\.tar\.gz)$"
        )
        for line in sums.splitlines():
            m = pattern.match(line.strip())
            if not m or m[3] not in SUPPORTED_PYTHONS:
                continue
            found[m[3]] = {"version": f"{m[3]}.{m[4]}", "file": m[2], "sha256": m[1]}
        missing = sorted(set(SUPPORTED_PYTHONS) - set(found))
        if missing:
            print(f"warning: {target.name}: no runtime for {', '.join(missing)}", file=sys.stderr)
        runtimes[target.name] = dict(sorted(found.items()))
    data = {"source": "python-build-standalone", "release": release, "base_url": BASE,
            "flavor": FLAVOR, "runtimes": runtimes}
    out = ROOT / "src" / "pyxbuild" / "runtimes.json"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
