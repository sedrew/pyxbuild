"""Build real applications with pyxbuild and exercise them on Windows.

For every application: generate a wrapper project that depends on it, build
the wrapper with pyxbuild on this host, run the built ``.exe`` on Windows
(natively or under $PYXBUILD_WINE) and record the outcome. The smoke test runs
inside the built application and uses the application for real: it serves
HTTP and requests a page, formats code, builds a site, crawls a local server.

    python tools/apps_matrix.py --jobs 3 --out docs/APPS.md --json apps.json
    python tools/apps_matrix.py --apps django,flask
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import platform
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Shared helpers, importable as ``appwrap._h`` from every smoke module.
HELPERS = r'''
import os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

RUNTIME = Path(sys.executable).parent

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def script(name):
    """A console script of a dependency (runtime\\Scripts\\<name>.exe)."""
    exe = RUNTIME / "Scripts" / (name + ".exe")
    assert exe.is_file(), f"missing {exe}"
    return str(exe)

def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()

def wait_http(url, timeout=240, proc=None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"server exited with {proc.returncode}:\n{out[-3000:]}")
        try:
            return get(url)
        except Exception as exc:  # not up yet
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"{url} did not answer: {last!r}")

def spawn(cmd, **kw):
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **kw)

def stop(proc):
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(30)
    except Exception:
        pass

def run(cmd, timeout=600, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, **kw)
    assert p.returncode == 0, f"{cmd} -> {p.returncode}\n{p.stdout[-2000:]}\n{p.stderr[-3000:]}"
    return p.stdout

def tmpdir():
    return Path(tempfile.mkdtemp(prefix="appwrap-"))

def serve_thread(server_run):
    import threading
    t = threading.Thread(target=server_run, daemon=True)
    t.start()
    return t
'''


@dataclass
class App:
    requirements: list[str]
    smoke: str                      # module source defining smoke() -> str
    files: dict[str, str] = field(default_factory=dict)  # extra modules in appwrap/
    config: dict[str, object] = field(default_factory=dict)  # [tool.pyxbuild] keys
    expect: str = "ok"              # "ok" or "fail" (known not to work on Windows)
    note: str = ""
    kind: str = ""


DJANGO_URLS = '''
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.utils import timezone

def hello(request):
    return JsonResponse({"hello": "мир", "now": timezone.localtime().isoformat()})

urlpatterns = [path("admin/", admin.site.urls), path("hello/", hello)]
'''

APPS: dict[str, App] = {
    "django": App(
        ["django", "waitress"], kind="web framework", files={"urls": DJANGO_URLS}, smoke='''
import json, os
from appwrap._h import *

def smoke():
    from django.conf import settings
    tmp = tmpdir()
    settings.configure(
        DEBUG=False, ALLOWED_HOSTS=["*"], SECRET_KEY="x" * 50, ROOT_URLCONF="appwrap.urls",
        INSTALLED_APPS=["django.contrib.admin", "django.contrib.auth",
                        "django.contrib.contenttypes", "django.contrib.sessions",
                        "django.contrib.messages", "django.contrib.staticfiles"],
        MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware",
                    "django.middleware.locale.LocaleMiddleware",
                    "django.middleware.common.CommonMiddleware",
                    "django.middleware.csrf.CsrfViewMiddleware",
                    "django.contrib.auth.middleware.AuthenticationMiddleware",
                    "django.contrib.messages.middleware.MessageMiddleware"],
        TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates",
                    "APP_DIRS": True, "OPTIONS": {"context_processors": [
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages"]}}],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                               "NAME": str(tmp / "db.sqlite3")}},
        USE_TZ=True, TIME_ZONE="Europe/Moscow", STATIC_URL="/static/",
        STATIC_ROOT=str(tmp / "static"), LANGUAGE_CODE="de")
    import django
    django.setup()
    from django.core.management import call_command
    call_command("check")
    call_command("migrate", verbosity=0)
    call_command("collectstatic", interactive=False, verbosity=0)
    assert (tmp / "static" / "admin" / "css" / "base.css").is_file()
    call_command("startproject", "mysite", str(tmp))  # templates are package data
    assert (tmp / "mysite" / "settings.py").is_file()
    from django.contrib.auth.models import User
    User.objects.create_superuser("admin", "a@example.org", "pw")
    from django.test import Client
    c = Client()
    r = c.post("/admin/login/?next=/admin/", {"username": "admin", "password": "pw"})
    assert r.status_code == 302, r.status_code
    r = c.get("/admin/")
    assert r.status_code == 200 and "Website-Verwaltung".encode() in r.content  # German .mo
    from django.core.wsgi import get_wsgi_application
    from waitress.server import create_server
    srv = create_server(get_wsgi_application(), host="127.0.0.1", port=0)
    serve_thread(srv.run)
    status, body = wait_http(f"http://127.0.0.1:{srv.effective_port}/hello/", 60)
    data = json.loads(body)
    assert data["hello"] == "мир" and data["now"].endswith("+03:00"), data  # tzdata
    srv.close()
    return django.get_version()
'''),
    "flask": App(
        ["flask", "waitress"], kind="web framework", smoke='''
from appwrap._h import *

def smoke():
    import flask
    from flask import Flask, jsonify, render_template_string, request
    from waitress.server import create_server
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string("<b>{{ x }}</b>", x=request.args.get("x"))

    @app.post("/echo")
    def echo():
        return jsonify(request.get_json())

    srv = create_server(app, host="127.0.0.1", port=0)
    serve_thread(srv.run)
    base = f"http://127.0.0.1:{srv.effective_port}"
    assert wait_http(base + "/?x=%3Ci%3E", 60)[1] == b"<b>&lt;i&gt;</b>"
    import urllib.request, json
    req = urllib.request.Request(base + "/echo", data=b'{"a": 1}',
                                 headers={"Content-Type": "application/json"})
    assert json.load(urllib.request.urlopen(req)) == {"a": 1}
    srv.close()
    from importlib.metadata import version
    return version("flask")
'''),
    "fastapi": App(
        ["fastapi", "uvicorn[standard]"], kind="web framework (ASGI)", smoke='''
import time
from appwrap._h import *

def smoke():
    import fastapi, uvicorn
    from fastapi import FastAPI
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str
        price: float

    app = FastAPI()

    @app.post("/items")
    def create(item: Item):
        return {"name": item.name.upper(), "price": item.price * 2}

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serve_thread(server.run)
    deadline = time.monotonic() + 60
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.2)
    import json, urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/items",
                                 data=b'{"name": "x", "price": 1.5}',
                                 headers={"Content-Type": "application/json"})
    assert json.load(urllib.request.urlopen(req)) == {"name": "X", "price": 3.0}
    assert b"swagger-ui" in get(f"http://127.0.0.1:{port}/docs")[1]
    server.should_exit = True
    import httptools, watchfiles, websockets  # the [standard] extras for win32
    return fastapi.__version__
'''),
    "datasette": App(
        ["datasette"], kind="data tool (ASGI + sqlite)", smoke='''
import json, sqlite3
from appwrap._h import *

def smoke():
    import datasette
    from click.testing import CliRunner
    from datasette.cli import cli
    db = tmpdir() / "data.db"
    con = sqlite3.connect(db)
    con.execute("create table t (id integer primary key, name text)")
    con.execute("insert into t (name) values ('привет')")
    con.commit(); con.close()
    r = CliRunner().invoke(cli, [str(db), "--get", "/data/t.json?_shape=array"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == [{"id": 1, "name": "привет"}], r.output
    r = CliRunner().invoke(cli, [str(db), "--get", "/data"])
    assert r.exit_code == 0 and "<html" in r.output, r.output[:500]
    return datasette.__version__
'''),
    "streamlit": App(
        ["streamlit"], kind="data app (tornado, pyarrow)", smoke='''
from appwrap._h import *

def smoke():
    tmp = tmpdir()
    (tmp / "app.py").write_text("import streamlit as st\\nst.write('hi')\\n")
    port = free_port()
    p = spawn([script("streamlit"), "run", str(tmp / "app.py"), "--server.headless", "true",
               "--server.port", str(port), "--browser.gatherUsageStats", "false"])
    try:
        assert wait_http(f"http://127.0.0.1:{port}/_stcore/health", proc=p)[1] == b"ok"
        assert b"<div id=\\"root\\">" in get(f"http://127.0.0.1:{port}/")[1]
    finally:
        stop(p)
    import streamlit
    return streamlit.__version__
'''),
    "gradio": App(
        ["gradio"], kind="ML demo UI (FastAPI + frontend)", smoke='''
import os
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
from appwrap._h import *

def smoke():
    import gradio as gr
    demo = gr.Interface(lambda s: s[::-1], "text", "text")
    port = free_port()
    demo.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True,
                quiet=True)
    try:
        status, body = get(f"http://127.0.0.1:{port}/")
        assert status == 200 and b"gradio" in body.lower()
        from gradio_client import Client
        assert Client(f"http://127.0.0.1:{port}/", verbose=False).predict(
            "abc", api_name="/predict") == "cba"
    finally:
        demo.close()
    return gr.__version__
'''),
    "wagtail": App(
        ["wagtail"], kind="CMS (Django, Pillow)", smoke='''
from appwrap._h import *

def smoke():
    tmp = tmpdir()
    run([script("wagtail"), "start", "mysite", str(tmp)])
    manage = str(tmp / "manage.py")
    run([sys.executable, manage, "migrate", "--noinput"], cwd=tmp, timeout=1200)
    out = run([sys.executable, manage, "check"], cwd=tmp)
    assert "no issues" in out, out
    # render the home page through the test client
    code = ("import os,django;os.environ.setdefault('DJANGO_SETTINGS_MODULE',"
            "'mysite.settings.dev');django.setup();from django.test import Client;"
            "r=Client().get('/');assert r.status_code==200,r.status_code;"
            "print(b'Welcome to your new Wagtail site' in r.content)")
    assert run([sys.executable, "-c", code], cwd=tmp).strip().endswith("True")
    import wagtail
    return wagtail.__version__
'''),
    "jupyterlab": App(
        ["jupyterlab"], kind="notebook server (tornado, zmq, pywinpty)", smoke='''
import json
from appwrap._h import *

def smoke():
    tmp = tmpdir()
    port = free_port()
    p = spawn([script("jupyter-lab"), "--no-browser", f"--port={port}",
               "--IdentityProvider.token=abc", f"--ServerApp.root_dir={tmp}"])
    try:
        status, body = wait_http(f"http://127.0.0.1:{port}/api/status?token=abc", proc=p)
        assert json.loads(body)["started"]
        status, body = get(f"http://127.0.0.1:{port}/lab?token=abc", 60)
        assert status == 200 and b"JupyterLab" in body
    finally:
        stop(p)
    # a real kernel: ipykernel in a child interpreter, talking over zmq
    from jupyter_client.manager import start_new_kernel
    km, kc = start_new_kernel(startup_timeout=240)
    try:
        reply = kc.execute_interactive("x = 6 * 7", timeout=120)
        assert reply["content"]["status"] == "ok"
        reply = kc.execute_interactive("assert x == 42", timeout=60)
        assert reply["content"]["status"] == "ok", reply["content"]
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)
    import jupyterlab
    return jupyterlab.__version__
'''),
    "httpie": App(
        ["httpie"], kind="CLI (requests, rich)", smoke='''
from appwrap._h import *

def smoke():
    out = run([script("http"), "--offline", "--print=HB", "example.org/api", "name=мир"])
    assert "POST /api HTTP/1.1" in out, out
    assert '"name": "\\u043c\\u0438\\u0440"' in out, out  # httpie escapes non-ASCII
    import httpie
    return httpie.__version__
'''),
    "black": App(
        ["black"], kind="CLI (mypyc-compiled)", smoke='''
from appwrap._h import *

def smoke():
    import black
    assert black.format_str("x=[1,2,\\n3]", mode=black.Mode()) == "x = [1, 2, 3]\\n"
    f = tmpdir() / "a.py"
    f.write_text("def f( a ):\\n  return a\\n")
    run([script("black"), "-q", str(f)])
    assert f.read_text() == "def f(a):\\n    return a\\n", f.read_text()
    assert black.COMPILED, "mypyc build not used"
    return black.__version__
'''),
    "ruff": App(
        ["ruff"], kind="native binary in a wheel", smoke='''
from appwrap._h import *

def smoke():
    from ruff.__main__ import find_ruff_bin
    f = tmpdir() / "a.py"
    f.write_text("import os\\n")
    p = subprocess.run([find_ruff_bin(), "check", "--select", "F401", "--no-cache", str(f)],
                       capture_output=True, text=True)
    assert p.returncode == 1 and "F401" in p.stdout, (p.returncode, p.stdout, p.stderr)
    return run([find_ruff_bin(), "--version"]).split()[1]
'''),
    "yt-dlp": App(
        ["yt-dlp[default]"], kind="CLI (lazy extractors)", smoke='''
from appwrap._h import *

def smoke():
    import yt_dlp
    from yt_dlp.extractor import gen_extractor_classes
    assert len(list(gen_extractor_classes())) > 1000
    out = run([script("yt-dlp"), "--version"])
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        assert ydl.params["quiet"]
    import certifi, brotli, websockets, mutagen  # the [default] extras
    return out.strip()
'''),
    "mkdocs-material": App(
        ["mkdocs-material"], kind="static site generator", smoke='''
from appwrap._h import *

def smoke():
    tmp = tmpdir()
    (tmp / "docs").mkdir()
    (tmp / "docs" / "index.md").write_text("# Привет\\n\\n!!! note\\n    text\\n",
                                          encoding="utf-8")
    (tmp / "mkdocs.yml").write_text(
        "site_name: t\\ntheme:\\n  name: material\\n  language: ru\\n"
        "markdown_extensions: [admonition]\\nplugins: [search]\\n")
    run([script("mkdocs"), "build", "-q", "-f", str(tmp / "mkdocs.yml")])
    html = (tmp / "site" / "index.html").read_text(encoding="utf-8")
    assert "admonition note" in html and "Привет" in html
    assert (tmp / "site" / "search" / "search_index.json").is_file()
    from importlib.metadata import version
    return version("mkdocs-material")
'''),
    "sphinx": App(
        ["sphinx", "furo"], kind="documentation generator", smoke='''
from appwrap._h import *

def smoke():
    import sphinx
    tmp = tmpdir()
    (tmp / "conf.py").write_text("project='t'\\nhtml_theme='furo'\\nlanguage='de'\\n"
                                 "extensions=['sphinx.ext.autodoc']\\n")
    (tmp / "index.rst").write_text("Title\\n=====\\n\\n"
                                   ".. automodule:: json\\n   :members: dumps\\n")
    run([script("sphinx-build"), "-q", "-W", "-b", "html", str(tmp), str(tmp / "out")])
    html = (tmp / "out" / "index.html").read_text(encoding="utf-8")
    assert "dumps" in html and "furo" in html
    assert "Suche" in html  # German translation catalogue
    return sphinx.__version__
'''),
    "scrapy": App(
        ["scrapy"], kind="crawler (twisted, lxml)", smoke='''
import http.server, threading
from appwrap._h import *

PAGE = b"<html><body><a href='/2'>next</a><p class='q'>one</p></body></html>"
PAGE2 = b"<html><body><p class='q'>two</p></body></html>"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE2 if self.path == "/2" else PAGE
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

def smoke():
    import scrapy
    from scrapy.crawler import CrawlerProcess
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    found = []

    class S(scrapy.Spider):
        name = "s"
        start_urls = [url]
        def parse(self, response):
            found.extend(response.css("p.q::text").getall())
            yield from response.follow_all(css="a", callback=self.parse)

    proc = CrawlerProcess({"LOG_LEVEL": "ERROR", "ROBOTSTXT_OBEY": False,
                           "TELNETCONSOLE_ENABLED": False})
    proc.crawl(S)
    proc.start()
    assert sorted(found) == ["one", "two"], found
    srv.shutdown()
    return scrapy.__version__
'''),
    "textual": App(
        ["textual"], kind="terminal UI", smoke='''
import asyncio
from appwrap._h import *

def smoke():
    import textual
    from textual.app import App as TApp
    from textual.widgets import Button, Input

    class A(TApp):
        def compose(self):
            yield Input(id="i")
            yield Button("Go", id="go")
        def on_button_pressed(self, event):
            self.exit(self.query_one(Input).value)

    async def drive():
        app = A()
        async with app.run_test() as pilot:
            await pilot.click("#i")
            await pilot.press("h", "i")
            await pilot.click("#go")
        return app.return_value

    assert asyncio.run(drive()) == "hi"
    from importlib.metadata import version
    return version("textual")
'''),
    "virtualenv": App(
        ["virtualenv"], kind="tool that runs the bundled interpreter", smoke='''
from appwrap._h import *

def smoke():
    from virtualenv import cli_run
    venv = tmpdir() / "venv"
    cli_run([str(venv), "--no-periodic-update", "--quiet"])
    py = venv / "Scripts" / "python.exe"
    out = run([str(py), "-c", "import sys, pip; print(sys.prefix != sys.base_prefix)"])
    assert out.strip() == "True", out
    import virtualenv
    return virtualenv.__version__
'''),
    "pygame": App(
        ["pygame-ce"], kind="game library (SDL2)", smoke='''
import os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
from appwrap._h import *

def smoke():
    import pygame
    pygame.init()
    screen = pygame.display.set_mode((64, 32))
    screen.fill((255, 0, 0))
    font = pygame.font.Font(None, 20)  # bundled default font (package data)
    screen.blit(font.render("hi", True, (0, 0, 0)), (2, 2))
    out = tmpdir() / "s.png"
    pygame.image.save(screen, str(out))
    img = pygame.image.load(str(out))
    assert img.get_size() == (64, 32) and img.get_at((60, 30))[:3] == (255, 0, 0)
    pygame.mixer.init()
    pygame.quit()
    return pygame.version.ver
'''),
    "locust": App(
        ["locust"], kind="load tester (gevent)", smoke='''
import http.server, threading
from appwrap._h import *

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def smoke():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tmpdir()
    (tmp / "lf.py").write_text(
        "from locust import HttpUser, task, constant\\n"
        "class U(HttpUser):\\n    wait_time = constant(0.1)\\n"
        "    @task\\n    def t(self):\\n        self.client.get('/')\\n")
    out = run([script("locust"), "-f", str(tmp / "lf.py"), "--headless", "-u", "2", "-r", "2",
               "-t", "5s", "--host", f"http://127.0.0.1:{srv.server_port}", "--only-summary",
               "--csv", str(tmp / "r")], timeout=300)
    rows = (tmp / "r_stats.csv").read_text().splitlines()
    total = [r for r in rows if ",Aggregated," in r][0].split(",")
    assert int(total[2]) > 5 and int(total[3]) == 0, total  # requests, failures
    import locust
    return locust.__version__
'''),
    "poetry": App(
        ["poetry"], kind="packaging tool (dulwich, keyring)", smoke='''
from appwrap._h import *

def smoke():
    tmp = tmpdir()
    out = run([script("poetry"), "--version"])
    run([script("poetry"), "new", "--no-interaction", "demo"], cwd=tmp)
    pyproject = (tmp / "demo" / "pyproject.toml").read_text()
    assert 'name = "demo"' in pyproject, pyproject
    run([script("poetry"), "check"], cwd=tmp / "demo")
    run([script("poetry"), "build", "-f", "wheel"], cwd=tmp / "demo")
    assert list((tmp / "demo" / "dist").glob("demo-*.whl"))
    return out.strip().split()[-1].rstrip(")")
'''),
    "kivy": App(
        ["kivy[base]"], kind="GUI framework (SDL2 DLL packages)", smoke='''
import os
os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_HOME"] = __import__("tempfile").mkdtemp()
from appwrap._h import *

def smoke():
    import kivy
    from kivy.core.text import Label as CoreLabel
    from kivy.core.image import Image as CoreImage
    lbl = CoreLabel(text="hi", font_size=20)
    lbl.refresh()
    assert lbl.texture.size[0] > 0
    from kivy.core import text
    assert text.Label.__name__ == "LabelSDL2", text.Label  # SDL2 from kivy_deps.sdl2
    return kivy.__version__
'''),
    "ansible": App(
        ["ansible-core"], kind="automation (POSIX-only)", expect="fail",
        note="Ansible does not support Windows as a control node.", smoke='''
from appwrap._h import *

def smoke():
    out = run([script("ansible"), "--version"])
    return out.split()[2]
'''),
}


@dataclass
class Result:
    app: str
    status: str  # ok | expected-fail | unexpected-ok | build-error | run-error | timeout
    detail: str = ""
    build_s: float = 0.0
    run_s: float = 0.0
    size_mb: float = 0.0
    files: int = 0
    version: str = ""
    packages: int = 0
    doctor: list[str] = field(default_factory=list)
    output: str = ""


def _toml_value(value: object) -> str:
    return json.dumps(value) if not isinstance(value, bool) else str(value).lower()


def make_project(dest: Path, name: str, app: App, python: str) -> Path:
    proj = dest / "app"
    pkg = proj / "appwrap"
    pkg.mkdir(parents=True)
    cfg = {"python": python, **app.config}
    (proj / "pyproject.toml").write_text(textwrap.dedent(f"""\
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"
        [project]
        name = "appwrap"
        version = "1.0"
        dependencies = {json.dumps(app.requirements)}
        [project.scripts]
        appwrap = "appwrap:main"
        [tool.pyxbuild]
        """) + "".join(f"{k} = {_toml_value(v)}\n" for k, v in cfg.items()),
        encoding="utf-8")
    (pkg / "_h.py").write_text(HELPERS, encoding="utf-8")
    (pkg / "smoke.py").write_text(textwrap.dedent(app.smoke), encoding="utf-8")
    for mod, src in app.files.items():
        (pkg / f"{mod}.py").write_text(textwrap.dedent(src), encoding="utf-8")
    (pkg / "__init__.py").write_text(textwrap.dedent("""\
        import sys, time, traceback

        def main():
            start = time.monotonic()
            try:
                from appwrap.smoke import smoke
                version = smoke()
            except BaseException:
                traceback.print_exc()
                print("APP-FAIL", flush=True)
                return 1
            print(f"APP-OK {version} ({time.monotonic() - start:.1f}s)", flush=True)
            return 0
        """), encoding="utf-8")
    return proj


def _short(text: str, limit: int = 200) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    interesting = [ln for ln in lines if any(k in ln for k in (
        "Error", "error:", "Exception", "no wheel", "has no wheels", "not found",
        "unimplemented", "assert"))]
    msg = (interesting[-1] if interesting else (lines[-1] if lines else "")).replace("|", "/")
    return msg[:limit]


def run_app(name: str, app: App, python: str, keep: Path | None, timeout: int) -> Result:
    with tempfile.TemporaryDirectory(prefix=f"pyxb-app-{name}-") as tmp:
        proj = make_project(Path(tmp), name, app, python)
        start = time.monotonic()
        build = subprocess.run([sys.executable, "-m", "pyxbuild", "build", str(proj)],
                               capture_output=True, text=True)
        build_s = time.monotonic() - start
        if build.returncode != 0:
            status = "expected-fail" if app.expect == "fail" else "build-error"
            return Result(name, status, _short(build.stderr), build_s,
                          output=build.stderr[-4000:])
        doctor = [ln.strip() for ln in (build.stdout + build.stderr).splitlines()
                  if ln.strip().startswith(("warning", "error", "!", "-"))][:20]
        out_dir = proj / "dist" / "appwrap-win_amd64"
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files) / 2**20
        lock = proj / "pylock.win_amd64.toml"
        packages = lock.read_text(encoding="utf-8").count("[[packages]]") if lock.exists() else 0
        cmd = [str(out_dir / "appwrap.exe")]
        if sys.platform != "win32":
            cmd.insert(0, os.environ.get("PYXBUILD_WINE", "wine"))
        env = {**os.environ, "WINEDEBUG": "-all"}
        start = time.monotonic()
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                                 encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            out, err, code = run.stdout, run.stderr, run.returncode
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(
                exc.stdout, bytes) else (exc.stdout or "")
            res = Result(name, "timeout", f"no result after {timeout}s", build_s,
                         time.monotonic() - start, size, len(files), packages=packages,
                         doctor=doctor, output=out[-4000:])
            return res
        run_s = time.monotonic() - start
        if keep is not None:
            import shutil
            shutil.copytree(out_dir, keep / name, dirs_exist_ok=True)
        ok = code == 0 and "APP-OK" in out
        version = out.split("APP-OK", 1)[1].strip().split(" (")[0] if ok else ""
        if app.expect == "fail":
            status = "unexpected-ok" if ok else "expected-fail"
        else:
            status = "ok" if ok else "run-error"
        detail = "" if ok else _short(err or out)
        return Result(name, status, detail, build_s, run_s, size, len(files), version,
                      packages, doctor, (out + "\n" + err)[-6000:])


ICON = {"ok": "✅", "expected-fail": "⛔ expected", "unexpected-ok": "❓ works",
        "build-error": "❌ build", "run-error": "⚠️ run", "timeout": "⏱ timeout"}


def render(results: list[Result], python: str, runner: str) -> str:
    by = {r.app: r for r in results}
    lines = [
        "| application | what it is | result | version | packages | folder | build | run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, app in APPS.items():
        r = by.get(name)
        if r is None:
            continue
        lines.append(
            f"| {name} | {app.kind} | {ICON[r.status]} | {r.version} | {r.packages or ''} | "
            f"{f'{r.size_mb:.0f} MB' if r.size_mb else ''} | {r.build_s:.0f} s | "
            f"{f'{r.run_s:.0f} s' if r.run_s else ''} |")
    problems = [r for r in results if r.status != "ok"]
    if problems:
        lines += ["", "| application | result | message |", "|---|---|---|"]
        for r in problems:
            lines.append(f"| {r.app} | {r.status} | `{r.detail}` |")
    head = (f"Generated by `tools/apps_matrix.py` on {time.strftime('%Y-%m-%d')}: Python "
            f"{python}, built on {platform.system()} {platform.machine()}, run on {runner}.")
    return head + "\n\n" + "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps", default=",".join(APPS))
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--out", type=Path, help="write the Markdown table here")
    parser.add_argument("--json", type=Path, help="write raw results (with output) here")
    parser.add_argument("--keep", type=Path, help="copy the built apps into this folder")
    parser.add_argument("--runner", default="Windows" if sys.platform == "win32" else "Wine")
    args = parser.parse_args()
    names = [a for a in args.apps.split(",") if a]
    unknown = [a for a in names if a not in APPS]
    if unknown:
        parser.error(f"unknown application(s): {', '.join(unknown)}")
    results: list[Result] = []
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_app, n, APPS[n], args.python, args.keep, args.timeout): n
                   for n in names}
        for fut in cf.as_completed(futures):
            name = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # keep going; report the crash
                res = Result(name, "build-error", _short(repr(exc)))
            results.append(res)
            print(f"{res.status:14} {name:16} build {res.build_s:4.0f}s run {res.run_s:4.0f}s "
                  f"{res.version} {res.detail}", flush=True)
            if args.json:
                args.json.write_text(json.dumps([asdict(r) for r in results], indent=1,
                                                ensure_ascii=False), encoding="utf-8")
    text = render(results, args.python, args.runner)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if all(r.status in ("ok", "expected-fail") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
