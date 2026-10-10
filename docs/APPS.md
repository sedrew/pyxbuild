# Real applications

The [compatibility matrix](COMPATIBILITY.md) proves that single packages with
compiled code load. This page goes one step further: whole applications,
used the way their users use them. `tools/apps_matrix.py` builds one wrapper
project per application (`dependencies = ["django", "waitress"]`, …) with
pyxbuild and runs a smoke test **inside the built `.exe`** that does real
work: start a server and request pages over HTTP, run migrations, render
templates, build a site, crawl a local server, start a Jupyter kernel.

```console
python tools/apps_matrix.py --jobs 3 --out apps.md --json apps.json   # build + run here
python tools/apps_matrix.py --build-only --keep apps-out              # build only
python tools/apps_matrix.py --run-from apps-out                       # run (e.g. on Windows)
```

CI builds all of them on ubuntu and runs them on `windows-latest`
(jobs `apps-build` / `apps-run`; the result table is in the job summary).

## Results

<!-- table -->

## What each smoke test does

| application | inside the built app |
|---|---|
| django | `check`, `migrate` into SQLite, `collectstatic`, `startproject` (package-data templates), admin login and dashboard through the test client in German (compiled `.mo`), then the WSGI app served by waitress and a JSON view requested over HTTP with `TIME_ZONE = "Europe/Moscow"` (needs `tzdata`, which Django requires only on Windows) |
| flask | Jinja2 autoescaping and a JSON POST, served by waitress |
| fastapi | uvicorn in a thread, pydantic validation over HTTP, `/docs`; `uvicorn[standard]` resolves `httptools`, `watchfiles`, `websockets` and skips `uvloop` (no Windows) |
| datasette | SQLite database with Cyrillic text queried through `datasette --get` (JSON and HTML) |
| streamlit | `runtime\Scripts\streamlit.exe run app.py` (console script of a dependency → pyxbuild launcher), `/_stcore/health` and the page |
| gradio | `Interface.launch()`, the page, and a prediction through `gradio_client` |
| wagtail | `wagtail start`, `manage.py migrate`/`check` with `sys.executable`, home page rendered |
| jupyterlab | `jupyter-lab.exe` server, `/api/status`, `/lab`; then a real ipykernel started by `jupyter_client` and code executed over ZeroMQ |
| httpie | `http.exe --offline` request with a JSON body |
| black | `format_str` and the `black.exe` CLI on a file; checks the mypyc-compiled wheel is used |
| ruff | the native `ruff.exe` shipped in the wheel's `.data/scripts` |
| yt-dlp | 1000+ lazy extractors, `yt-dlp.exe --version`, the `[default]` extras |
| mkdocs-material | `mkdocs.exe build` with the Material theme in Russian, admonitions, search index |
| sphinx | `sphinx-build.exe -W` with the furo theme, autodoc and the German catalogue |
| scrapy | a spider following links on a local HTTP server (Twisted asyncio reactor, lxml, parsel) |
| textual | an app driven by the test pilot: click, type, button |
| virtualenv | creates a venv from the bundled `python.exe` and imports pip in it |
| pygame | `pygame-ce` with SDL dummy drivers: draw, bundled font, save/load PNG, mixer |
| locust | `locust.exe --headless` (gevent) against a local server; checks request/failure counts |
| poetry | `poetry new`, `check`, `build` (dulwich, keyring, virtualenv) |
| kivy | `kivy[base]`: SDL2 text/image providers from the `kivy_deps.*` DLL packages load. Rendering needs a window with an OpenGL context, so it is not exercised headless |
| ansible | `ansible-core` is **expected to fail**: Ansible does not support Windows as a control node (`os.get_blocking` at CLI start-up). `pyxbuild doctor` now warns about it |

## Findings

**pyxbuild bug fixed — files shared by several wheels.** `kivy[base]` did not
build: `kivy_deps.sdl2`, `kivy_deps.glew` and `kivy_deps.angle` each ship the
same pkgutil namespace file `kivy_deps/__init__.py`, and pyxbuild refused to
overwrite it. pip lets the last wheel win. pyxbuild now accepts a file that
already exists with identical bytes (both `RECORD`s list it, as with pip)
and still refuses different content, which means two packages provide the
same module (`opencv-python` + `opencv-python-headless`, `jwt` + `PyJWT`).
Old namespace packages (`zope.*`, `google.*` before PEP 420,
`backports.*`) hit the same case.

**pyxbuild doctor — ansible-core.** Added to the known-issues list: it
builds, then fails at start-up on Windows.

**Things that worked without any configuration** and are classic
PyInstaller support questions (hidden imports, data files, hooks):

* Django admin templates, static files, migrations, `startproject`
  templates and translations — package data is simply there;
* `tzdata` pulled in by Django's `sys_platform == "win32"` marker;
* uvicorn's dynamically imported protocol/loop modules;
* `importlib.metadata` version lookups (Streamlit, Gradio, Jupyter,
  Poetry plugins, Sphinx extensions, entry-point based plugin systems such
  as mkdocs themes and Jupyter server extensions);
* console scripts of dependencies (`streamlit.exe`, `jupyter-lab.exe`,
  `mkdocs.exe`, `sphinx-build.exe`, `poetry.exe`, `locust.exe`) — the
  pyxbuild launcher finds `..\python.exe`;
* `sys.executable` subprocesses: Wagtail's `manage.py`, Jupyter kernels,
  virtualenv;
* native executables inside wheels (`ruff.exe`) and mypyc-compiled wheels
  (black);
* gevent and Twisted event loops on Windows.

**Test-harness lessons (not pyxbuild bugs):**

* Under Wine, background processes started on demand (`wineserver`,
  `services.exe`, `explorer.exe /desktop` when something creates a window)
  inherit the stdout pipe of the process that started them, so reading the
  app's output never reaches end-of-file. The tool starts `wineserver -p`
  and Wine's services before the first app.
* `import locust` monkey-patches the process with gevent; doing it after a
  real thread has started deadlocks. The smoke test reads the version from
  metadata instead.
* httpie escapes non-ASCII characters in JSON bodies; Gradio 6 names API
  endpoints after the function.

## Adding an application

Add an entry to `APPS` in `tools/apps_matrix.py`: the requirements, a
`smoke()` function that exercises the application and returns its version,
optionally extra modules (`files`) and `[tool.pyxbuild]` settings
(`config`). Helpers in `appwrap._h`: `free_port()`, `script(name)` (a
dependency's console script), `get(url)`, `wait_http(url, proc=...)`,
`spawn(cmd)`, `run(cmd)`, `tmpdir()`. A hang prints the Python stack of
every thread (`faulthandler`) before the timeout.
