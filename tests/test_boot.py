import importlib.util
import json
import sys

import pytest

from conftest import ROOT


@pytest.fixture
def boot():
    path = ROOT / "src" / "pyxbuild" / "_boot" / "_pyxbuild_boot.py"
    spec = importlib.util.spec_from_file_location("_pyxbuild_boot_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prefix(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "app.py").write_text(
        "import os, sys\n"
        "class App:\n"
        "    @staticmethod\n"
        "    def run():\n"
        "        print('ARGV', sys.argv)\n"
        "        print('DATA', os.environ.get('MY_DATA'))\n"
        "        print('TCL', os.environ.get('TCL_LIBRARY'))\n"
        "        return 7\n"
        "def crash():\n"
        "    print('before crash')\n"
        "    raise ValueError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


def write_cfg(prefix, **boot):
    cfg = {"project": {"name": "MyApp"},
           "launchers": {"cli": {"entry-point": "pkg.app:App.run", "gui": False},
                         "gui": {"entry-point": "pkg.app:crash", "gui": True}},
           "boot": boot}
    (prefix / "pyxbuild.json").write_text(json.dumps(cfg))


def test_runs_entry_point_with_env(boot, prefix, monkeypatch, capsys):
    write_cfg(prefix, **{"clean-env": ["TCL_LIBRARY"],
                         "env": {"MY_DATA": "{data_dir}\\models"}})
    monkeypatch.setenv("TCL_LIBRARY", r"C:\Python27\tcl")
    monkeypatch.setattr(sys, "argv", ["-c", r"C:\apps\MyApp\cli.exe", "x"])
    with pytest.raises(SystemExit) as exc:
        boot.main("cli")
    assert exc.value.code == 7
    out = capsys.readouterr().out
    assert "ARGV ['C:\\\\apps\\\\MyApp\\\\cli.exe', 'x']" in out
    assert f"DATA {prefix / 'local' / 'MyApp'}\\models" in out
    assert "TCL None" in out


def test_gui_without_console_logs_and_reports(boot, prefix, monkeypatch):
    write_cfg(prefix)
    monkeypatch.setenv("PYXBUILD_BOOT_NO_DIALOG", "1")
    monkeypatch.setattr(sys, "argv", ["-c", "gui.exe"])
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    with pytest.raises(ValueError) as exc:
        boot.main("gui")
    sys.excepthook(ValueError, exc.value, exc.value.__traceback__)
    sys.stdout.flush()
    log = (prefix / "local" / "MyApp" / "logs" / "gui.log").read_text()
    assert "before crash" in log
    assert "ValueError: boom" in log


def test_concurrent_log_appends_lose_no_lines(tmp_path):
    """Several processes write the GUI log at once (multiprocessing workers):
    on Windows the C runtime's "a" mode lets them overwrite each other."""
    import subprocess

    streams = ROOT / "src" / "pyxbuild" / "_boot"
    log = tmp_path / "app.log"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from _pyxbuild_streams import open_append; "
        "f = open_append(sys.argv[2]); "
        "[f.write(f'{sys.argv[3]} line {i}\\n') for i in range(300)]; f.close()"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(streams), str(log), f"p{n}"])
             for n in range(6)]
    for p in procs:
        assert p.wait(60) == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == sorted(f"p{n} line {i}" for n in range(6) for i in range(300))
