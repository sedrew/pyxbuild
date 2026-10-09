import shutil
import subprocess
import sys

import pytest

from conftest import ROOT, make_pe
from pyxbuild import pe
from pyxbuild.config import EntryPoint
from pyxbuild.errors import PyxbuildError
from pyxbuild.launcher import (
    bootstrap_code,
    build_launcher,
    decode_manifest,
    encode_manifest,
    stub_bytes,
)

ENTRY = EntryPoint("app", "pkg.main", "App.run", gui=False)


@pytest.mark.parametrize(("gui", "subsystem"), [(False, pe.SUBSYSTEM_CONSOLE),
                                                (True, pe.SUBSYSTEM_GUI)])
def test_stubs_are_amd64_pe_with_expected_subsystem(gui, subsystem):
    info = pe.parse(stub_bytes("win_amd64", gui))
    assert info.machine == pe.IMAGE_FILE_MACHINE_AMD64
    assert info.is_pe32_plus
    assert info.subsystem == subsystem
    assert not info.is_dll
    assert "KERNEL32.dll".lower() in [i.lower() for i in info.imports]


def test_stub_size_is_small():
    assert len(stub_bytes("win_amd64", False)) < 128 * 1024


def test_manifest_roundtrip():
    exe = build_launcher(ENTRY, "win_amd64", python_rel="runtime")
    fields = decode_manifest(exe)
    assert fields["python"] == "runtime\\python.exe"
    assert fields["flags"] == "-I"
    assert fields["entry-point"] == "pkg.main:App.run"
    assert fields["code"] == bootstrap_code(ENTRY)


def test_gui_launcher_uses_pythonw():
    exe = build_launcher(EntryPoint("g", "m", "f", gui=True), "win_amd64", python_rel="..")
    assert decode_manifest(exe)["python"] == "..\\pythonw.exe"
    assert pe.parse(exe).subsystem == pe.SUBSYSTEM_GUI


def test_bootstrap_code_runs_entry_point(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "main.py").write_text(
        "import sys\nclass App:\n    @staticmethod\n    def run():\n"
        "        print(repr(sys.argv))\n        return 3\n")
    proc = subprocess.run([sys.executable, "-c", bootstrap_code(ENTRY), "C:\\app.exe", "a b"],
                          cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 3
    assert proc.stdout.strip() == repr(["C:\\app.exe", "a b"])


def test_bootstrap_code_has_no_double_quotes():
    assert '"' not in bootstrap_code(ENTRY)


def test_manifest_rejects_newlines():
    with pytest.raises(PyxbuildError):
        encode_manifest({"code": "a\nb"})


def test_decode_rejects_plain_exe():
    with pytest.raises(PyxbuildError):
        decode_manifest(stub_bytes("win_amd64", False))


@pytest.mark.skipif(not shutil.which("osslsigncode") or not shutil.which("openssl"),
                    reason="needs osslsigncode and openssl")
def test_manifest_survives_authenticode_signing(tmp_path):
    from pyxbuild.sign import sign_file

    exe = tmp_path / "app.exe"
    exe.write_bytes(build_launcher(ENTRY, "win_amd64", python_rel="runtime"))
    pfx = make_test_certificate(tmp_path)
    sign_file(exe, pfx, password="test")
    signed = exe.read_bytes()
    assert pe.security_directory(signed) is not None
    assert decode_manifest(signed)["entry-point"] == "pkg.main:App.run"
    proc = subprocess.run(["osslsigncode", "verify", "-in", str(exe)], capture_output=True,
                          text=True)
    assert "Signature verification: ok" in proc.stdout or "Message digest" in proc.stdout


def make_test_certificate(tmp_path):
    key, crt, pfx = tmp_path / "k.pem", tmp_path / "c.pem", tmp_path / "c.pfx"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                    str(key), "-out", str(crt), "-days", "2", "-subj", "/CN=pyxbuild test",
                    "-addext", "extendedKeyUsage=codeSigning"], check=True, capture_output=True)
    subprocess.run(["openssl", "pkcs12", "-export", "-inkey", str(key), "-in", str(crt),
                    "-out", str(pfx), "-passout", "pass:test"], check=True, capture_output=True)
    return pfx


def test_pe_parser_imports_and_delay_imports():
    data = make_pe(["KERNEL32.dll", "python312.dll"], delay=["libfoo.dll"])
    info = pe.parse(data)
    assert info.imports == ["KERNEL32.dll", "python312.dll"]
    assert info.delay_imports == ["libfoo.dll"]
    assert info.is_dll


def test_pe_parser_rejects_garbage():
    with pytest.raises(pe.PEError):
        pe.parse(b"not a pe file at all" * 10)
    with pytest.raises(pe.PEError):
        pe.parse(make_pe(["a.dll"])[:300])


@pytest.mark.skipif(shutil.which("zig") is None and not __import__("importlib.util").util
                    .find_spec("ziglang"), reason="needs zig (pip install ziglang)")
def test_committed_stubs_match_source():
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "build_stubs.py"), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
