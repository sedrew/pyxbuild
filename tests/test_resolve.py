"""The built-in resolver (pyxbuild.resolve) and in-process wheel builder (purewheel),
used where uv cannot run (the browser sandbox)."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile

import pytest

from pyxbuild import resolve
from pyxbuild.errors import PyxbuildError, ResolveError
from pyxbuild.lock import read_lock, select_files
from pyxbuild.purewheel import build_pure_wheel
from pyxbuild.targets import WIN_AMD64

PY = "3.12.10"
TAGS = WIN_AMD64.tags("3.12")
ENV = WIN_AMD64.marker_environment(PY)
INDEX = "https://index.test/simple"


def _wheel(name: str, version: str, requires: list[str],
           tag: str = "py3-none-any") -> tuple[str, bytes, bytes]:
    dist = name.replace("-", "_")
    filename = f"{dist}-{version}-{tag}.whl"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {r}\n" for r in requires)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{dist}/__init__.py", "")
        zf.writestr(f"{dist}-{version}.dist-info/METADATA", meta)
    return filename, buf.getvalue(), meta.encode()


class FakeIndex:
    """name -> list of (version, requires, tag, extra file fields, pep658)."""

    def __init__(self, packages: dict[str, list[tuple]]) -> None:
        self.responses: dict[str, bytes] = {}
        self.requests: list[str] = []
        for name, releases in packages.items():
            files = []
            for version, requires, tag, extra, pep658 in releases:
                filename, data, meta = _wheel(name, version, requires, tag)
                url = f"https://files.test/{filename}"
                self.responses[url] = data
                entry = {"filename": filename, "url": url,
                         "hashes": {"sha256": hashlib.sha256(data).hexdigest()},
                         "size": len(data), **extra}
                if pep658:
                    self.responses[url + ".metadata"] = meta
                    entry["core-metadata"] = {"sha256": hashlib.sha256(meta).hexdigest()}
                files.append(entry)
            self.responses[f"{INDEX}/{name}/"] = json.dumps({"files": files}).encode()

    def __call__(self, url: str, headers: dict[str, str]) -> bytes:
        self.requests.append(url)
        if url not in self.responses:
            raise OSError(f"404 {url}")
        return self.responses[url]


def rel(version, requires=(), tag="py3-none-any", pep658=True, **extra):
    return (version, list(requires), tag, extra, pep658)


def _resolve(index: FakeIndex, reqs: list[str]) -> dict[str, str]:
    pins = resolve.resolve(reqs, TAGS, ENV, PY, index_url=INDEX, get=index)
    return {p.name: p.version for p in pins}


def test_picks_newest_versions_with_a_wheel_for_the_target():
    index = FakeIndex({
        "app": [rel("1.0", ["lib>=1"]), rel("2.0", ["lib>=1"])],
        "lib": [rel("1.0", tag="cp312-cp312-win_amd64"),
                rel("2.0", tag="cp312-cp312-manylinux_2_17_x86_64")],  # no Windows wheel
    })
    assert _resolve(index, ["app"]) == {"app": "2.0", "lib": "1.0"}
    # only metadata was fetched, never a wheel
    assert not any(u.endswith(".whl") for u in index.requests)


def test_markers_extras_requires_python_yanked_and_prereleases():
    index = FakeIndex({
        "app": [rel("1.0", ["linuxonly; sys_platform == 'linux'",
                            "winonly; sys_platform == 'win32'",
                            "fast; extra == 'speed'"])],
        "winonly": [rel("1.0"), rel("2.0", **{"requires-python": ">=3.13"}),
                    rel("1.5", yanked=True), rel("3.0a1")],
        "fast": [rel("1.0")],
        "linuxonly": [rel("1.0")],
    })
    assert _resolve(index, ["app"]) == {"app": "1.0", "winonly": "1.0"}
    assert _resolve(index, ["app[speed]"]) == {"app": "1.0", "winonly": "1.0", "fast": "1.0"}
    assert _resolve(index, ["app", "winonly>=3.0a1"])["winonly"] == "3.0a1"


def test_backtracks_and_reports_conflicts():
    index = FakeIndex({
        "a": [rel("1.0", ["c<2"]), rel("2.0", ["c>=3"])],
        "b": [rel("1.0", ["c<2"])],
        "c": [rel("1.0"), rel("3.0")],
    })
    assert _resolve(index, ["a", "b"]) == {"a": "1.0", "b": "1.0", "c": "1.0"}
    with pytest.raises(ResolveError, match="cannot resolve"):
        _resolve(index, ["a>=2", "b"])


def test_metadata_from_the_wheel_without_pep658():
    index = FakeIndex({"app": [rel("1.0", ["lib"], pep658=False)], "lib": [rel("1.0")]})
    assert _resolve(index, ["app"]) == {"app": "1.0", "lib": "1.0"}


def test_lock_text_is_read_and_selected_like_uv_locks(tmp_path):
    index = FakeIndex({"app": [rel("1.0", ["lib"])],
                       "lib": [rel("1.0", tag="cp312-cp312-win_amd64"),
                               rel("1.0", tag="py3-none-any")]})
    pins = resolve.resolve(["app"], TAGS, ENV, PY, index_url=INDEX, get=index)
    path = tmp_path / "pylock.win_amd64.toml"
    path.write_text(resolve.pylock_text(pins, INDEX))
    chosen = select_files(read_lock(path), WIN_AMD64, ENV, TAGS)
    assert sorted(s.file.name for s in chosen) == [
        "app-1.0-py3-none-any.whl", "lib-1.0-cp312-cp312-win_amd64.whl"]


def test_unsupported_inputs_are_refused():
    with pytest.raises(ResolveError, match="find-links"):
        resolve.check_supported(find_links=["./wheels"], extra_index_urls=[], no_binary=[])


def test_pure_wheel_from_src_layout(tmp_path):
    (tmp_path / "src" / "my_app").mkdir(parents=True)
    (tmp_path / "src" / "my_app" / "__init__.py").write_text("def main(): pass\n")
    (tmp_path / "src" / "my_app" / "data.txt").write_text("x")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "My-App"\nversion = "1.2"\ndescription = "Тест"\n'
        'dependencies = ["rich; os_name == \'nt\'"]\n'
        '[project.optional-dependencies]\nfast = ["orjson"]\n'
        '[project.gui-scripts]\nmy-app = "my_app:main"\n')
    wheel = build_pure_wheel(tmp_path, tmp_path / "out")
    assert wheel.name == "my_app-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        meta = zf.read("my_app-1.2.dist-info/METADATA").decode()
        eps = zf.read("my_app-1.2.dist-info/entry_points.txt").decode()
    assert {"my_app/__init__.py", "my_app/data.txt", "my_app-1.2.dist-info/RECORD"} <= names
    assert "Requires-Dist: orjson ; extra == \"fast\"" in meta and "Summary: Тест" in meta
    assert "[gui_scripts]\nmy-app = my_app:main" in eps
    # the installer accepts it (RECORD hashes are right)
    from pyxbuild.install import install_wheel

    install_wheel(wheel, tmp_path / "site")
    assert [p.read_text() for p in (tmp_path / "site").rglob("my_app/data.txt")] == ["x"]


def test_pure_wheel_refuses_what_needs_a_backend(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndynamic = ["version"]\n')
    with pytest.raises(PyxbuildError, match="version"):
        build_pure_wheel(tmp_path, tmp_path / "out")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1"\n')
    with pytest.raises(PyxbuildError, match="cannot find the code"):
        build_pure_wheel(tmp_path, tmp_path / "out")


@pytest.mark.network
def test_same_versions_as_uv():
    reqs = ["requests[socks]", "pywin32; sys_platform == 'win32'", "colorama; os_name == 'nt'",
            "httpx[http2]", "rich", "numpy"]
    ours = {p.name: p.version for p in resolve.resolve(reqs, TAGS, ENV, PY)}
    from pyxbuild.lock import uv_executable

    out = subprocess.run([uv_executable(), "pip", "compile", "-", "--python-platform",
                          WIN_AMD64.uv_platform, "--python-version", PY, "--only-binary",
                          ":all:", "--no-header", "--no-annotate", "-q", "--no-config"],
                         input="\n".join(reqs), capture_output=True, text=True, check=True)
    from packaging.utils import canonicalize_name

    theirs = {canonicalize_name(n): v.split(";")[0].strip()
              for n, v in (line.split("==") for line in out.stdout.splitlines() if "==" in line)}
    assert ours == theirs

