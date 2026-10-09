"""Authenticode signing from any OS through ``osslsigncode`` (or ``jsign``)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .errors import PyxbuildError


def _tool() -> tuple[str, str]:
    for name in ("osslsigncode", "jsign"):
        exe = shutil.which(name)
        if exe:
            return name, exe
    raise PyxbuildError(
        "no signing tool found",
        hint="install osslsigncode (apt/brew/choco install osslsigncode) or jsign",
    )


def sign_file(path: Path, cert: Path, *, password: str | None = None,
              timestamp_url: str | None = None, description: str | None = None,
              url: str | None = None) -> None:
    """Sign ``path`` in place with a PKCS#12 certificate (``.pfx``/``.p12``)."""
    kind, exe = _tool()
    tmp = path.with_name(path.name + ".signed")
    if kind == "osslsigncode":
        cmd = [exe, "sign", "-pkcs12", str(cert), "-h", "sha256"]
        if password is not None:
            cmd += ["-pass", password]
        if description:
            cmd += ["-n", description]
        if url:
            cmd += ["-i", url]
        if timestamp_url:
            cmd += ["-ts", timestamp_url]
        cmd += ["-in", str(path), "-out", str(tmp)]
    else:
        cmd = [exe, "--keystore", str(cert), "--storetype", "PKCS12", "--alg", "SHA-256"]
        if password is not None:
            cmd += ["--storepass", password]
        if description:
            cmd += ["--name", description]
        if url:
            cmd += ["--url", url]
        if timestamp_url:
            cmd += ["--tsaurl", timestamp_url]
        cmd.append(str(path))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise PyxbuildError(f"signing {path.name} failed:\n{(proc.stderr or proc.stdout).strip()}")
    if kind == "osslsigncode":
        os.replace(tmp, path)


def launchers_in(app_dir: Path) -> list[Path]:
    """The launchers pyxbuild created in an application folder."""
    from .launcher import decode_manifest

    out = []
    for exe in sorted(app_dir.glob("*.exe")):
        try:
            decode_manifest(exe.read_bytes())
        except (PyxbuildError, ValueError):
            continue
        out.append(exe)
    return out
