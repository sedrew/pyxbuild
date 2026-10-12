"""Apply a pyxbuild patch (see src/pyxbuild/patch.py).

A patch executable extracts its payload (this module, ``patch.json`` and the
changed files under ``blobs/``) to a temporary folder and runs this module
with the Python of the installed version the patch was made for. It
assembles the new version in the onefile cache, next to the old one:

* files whose content the old version already has are hard-linked (or
  copied) from it after checking their sha256 (the cache is user-writable);
* new files come from ``blobs/<sha256>``, or, for files of public wheels
  when the patch was made with ``--fetch-public``, from the wheel downloaded
  from the URL in the lock file and checked against its sha256;
* every file written is checked against the hash the new version expects;
* the folder is renamed into place only when complete (``.pyx-complete``),
  and ``.pyx-next`` in the old folder points older executables at it.

The result is the folder the new full executable would extract itself.
Standard library only.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile


class PatchError(Exception):
    pass


def _sha256(path, cache={}):  # noqa: B006 - deliberate per-run memo
    if path not in cache:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while block := f.read(1 << 20):
                h.update(block)
        cache[path] = h.hexdigest()
    return cache[path]


def _mutex(name):
    """The launcher's extraction mutex, so a full executable of the same
    version never extracts into the folder while we assemble it."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, name)
        if handle:
            kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
        return kernel32, handle
    except (AttributeError, OSError):  # not Windows (tests)
        return None, None


def _read12(path):
    with open(path, encoding="ascii", errors="replace") as f:
        return f.read(12)


def _release(mutex):
    kernel32, handle = mutex
    if handle:
        import ctypes

        kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _download(url, sha256, size, dest):
    print(f"  downloading {url.rsplit('/', 1)[-1]}", flush=True)
    h = hashlib.sha256()
    got = 0
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        while block := resp.read(1 << 20):
            got += len(block)
            if size is not None and got > size:
                raise PatchError(f"{url}: larger than expected")
            h.update(block)
            out.write(block)
    if h.hexdigest() != sha256:
        raise PatchError(f"{url}: sha256 mismatch")


def _place(src, dst, link):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if link:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass  # other volume, FAT, no permission: copy
    shutil.copyfile(src, dst)


def apply(payload, appdir, log=print):
    with open(os.path.join(payload, "patch.json"), encoding="utf-8") as f:
        patch = json.load(f)
    if patch.get("format") != 1:
        raise PatchError("unsupported patch format")
    base = os.path.join(appdir, patch["base"][:12])
    target12 = patch["target"][:12]
    target = os.path.join(appdir, target12)
    if not os.path.isfile(os.path.join(base, ".pyx-complete")):
        raise PatchError(f"the version this patch is for is not installed: {base}")

    mutex = _mutex("Local\\pyx-onefile-" + target12)
    try:
        if os.path.isfile(os.path.join(target, ".pyx-complete")):
            log(f"{patch['app']} {patch['version']} is already installed")
        else:
            _assemble(patch, payload, base, target, log)
    finally:
        _release(mutex)

    # older executables follow .pyx-next to the newest version; also re-point
    # versions that pointed at our base so that chains stay one hop long
    base12 = patch["base"][:12]
    for name in os.listdir(appdir):
        nxt = os.path.join(appdir, name, ".pyx-next")
        if name != base12 and not (os.path.isfile(nxt) and _read12(nxt) == base12):
            continue
        with open(nxt, "w", encoding="ascii") as f:
            f.write(target12 + "\n")
    log(f"updated {patch['app']} to {patch['version']}")
    return target


def _assemble(patch, payload, base, target, log):
    files = patch["files"]
    work = f"{target}.tmp-{os.getpid():x}-patch"
    if os.path.exists(work):
        shutil.rmtree(work)
    downloads = tempfile.mkdtemp(prefix="pyx-patch-wheels-")
    try:
        # 1. the old version's files we reuse must still be what we expect
        keep = patch["keep"]
        moved = [e for e in files if e["from"] == "base"]
        log(f"  checking {len(keep) + len(moved)} unchanged file(s)")
        digest = hashlib.sha256()
        for path in keep:
            src = os.path.join(base, *path.split("/"))
            if not os.path.isfile(src):
                raise PatchError(f"the installed version is damaged ({path} is missing); "
                                 "install the full version")
            digest.update(_sha256(src).encode())
        if digest.hexdigest() != patch["keep_sha256"]:
            raise PatchError("the installed version is damaged; install the full version")
        for e in moved:
            src = os.path.join(base, *e["src"].split("/"))
            if not os.path.isfile(src) or _sha256(src) != e["sha256"]:
                raise PatchError(f"the installed version is damaged ({e['src']}); "
                                 "install the full version")
        # 2. wheels with public files (--fetch-public)
        wheels = {}
        for i, w in enumerate(patch.get("wheels", [])):
            path = os.path.join(downloads, w["url"].rsplit("/", 1)[-1])
            _download(w["url"], w["sha256"], w.get("size"), path)
            wheels[i] = zipfile.ZipFile(path)
        # 3. assemble
        log(f"  writing {len(keep) + len(files)} file(s)")
        for path in keep:
            _place(os.path.join(base, *path.split("/")),
                   os.path.join(work, *path.split("/")), link=True)
        for e in files:
            dst = os.path.join(work, *e["path"].split("/"))
            if e["from"] == "base":
                _place(os.path.join(base, *e["src"].split("/")), dst, link=True)
                continue
            if e["from"] == "payload":
                _place(os.path.join(payload, "blobs", e["sha256"]), dst, link=False)
            else:
                index, member = e["src"]
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with wheels[index].open(member) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
            if os.path.getsize(dst) != e["size"] or _sha256(dst) != e["sha256"]:
                raise PatchError(f"{e['path']}: content does not match the new version")
        for z in wheels.values():
            z.close()
        with open(os.path.join(work, ".pyx-complete"), "w", encoding="utf-8") as f:
            f.write(patch["target"] + "\n")
        try:
            os.rename(work, target)
        except OSError:
            if not os.path.isfile(os.path.join(target, ".pyx-complete")):
                if os.path.exists(target):  # an incomplete earlier attempt
                    shutil.rmtree(target)
                    os.rename(work, target)
                else:
                    raise
    finally:
        shutil.rmtree(downloads, ignore_errors=True)
        if os.path.exists(work):
            shutil.rmtree(work, ignore_errors=True)


def main():
    try:
        apply(os.environ["PYX_PATCH_PAYLOAD"], os.environ["PYX_PATCH_APPDIR"])
    except (PatchError, OSError) as exc:
        print(f"update failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0
