// pyxbuild in the browser: a web worker that runs the real pyxbuild in Pyodide.
//
// Synchronous XMLHttpRequest is allowed in workers, so pyxbuild's code stays
// synchronous (src/pyxbuild/download.py: _browser_get). Next to this file the
// site build (tools/build_site.py) puts:
//   pyodide/          the Pyodide distribution (core files)
//   wheels/           pyxbuild and its pure-Python dependencies + wheels.json
//   ../../runtimes/   python-build-standalone archives (GitHub releases do not
//                     allow cross-origin downloads, the site does)
/* global loadPyodide */
"use strict";

importScripts("pyodide/pyodide.js");

let pyodide = null;
let ready = null;

function post(type, data) {
  self.postMessage({ type, ...data });
}

async function init() {
  post("log", { line: "Loading Python (Pyodide)…" });
  pyodide = await loadPyodide({ indexURL: new URL("pyodide/", self.location).href });
  pyodide.setStdout({ batched: (line) => post("log", { line }) });
  pyodide.setStderr({ batched: (line) => post("log", { line }) });
  const wheelsBase = new URL("wheels/", self.location);
  const list = await (await fetch(new URL("wheels.json", wheelsBase))).json();
  pyodide.FS.mkdirTree("/wheels");
  for (const name of list) {
    const data = new Uint8Array(await (await fetch(new URL(name, wheelsBase))).arrayBuffer());
    pyodide.FS.writeFile(`/wheels/${name}`, data);
  }
  pyodide.runPython(`
import glob, site, zipfile
for w in sorted(glob.glob("/wheels/*.whl")):
    zipfile.ZipFile(w).extractall(site.getsitepackages()[0])
import pyxbuild
`);
  const version = pyodide.runPython("pyxbuild.__version__");
  post("ready", { version });
}

function writeTree(root, files) {
  for (const [path, content] of Object.entries(files)) {
    const full = `${root}/${path}`;
    pyodide.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
    pyodide.FS.writeFile(full, content);
  }
}

function readTree(dir, prefix = "") {
  const out = {};
  for (const name of pyodide.FS.readdir(dir)) {
    if (name === "." || name === "..") continue;
    const full = `${dir}/${name}`;
    const rel = prefix ? `${prefix}/${name}` : name;
    if (pyodide.FS.isDir(pyodide.FS.stat(full).mode)) Object.assign(out, readTree(full, rel));
    else out[rel] = pyodide.FS.readFile(full);
  }
  return out;
}

async function build({ files, args }) {
  await ready;
  const work = `/work/${Date.now()}`;
  writeTree(`${work}/project`, files);
  const runtimes = new URL("../../runtimes", self.location).href;
  const argv = ["build", `${work}/project`, "--out", `${work}/out`, ...args];
  pyodide.globals.set("argv", pyodide.toPy(argv));
  pyodide.globals.set("runtime_mirror", runtimes);
  const started = performance.now();
  const rc = pyodide.runPython(`
import os
os.environ["PYXBUILD_RUNTIME_MIRROR"] = runtime_mirror
os.environ.setdefault("PYXBUILD_CACHE_DIR", "/cache")
from pyxbuild import cli
try:
    rc = cli.main(list(argv))
except SystemExit as exc:
    rc = exc.code if isinstance(exc.code, int) else 1
rc
`);
  const seconds = ((performance.now() - started) / 1000).toFixed(1);
  if (rc !== 0) {
    post("failed", { code: rc, seconds });
    return;
  }
  const out = readTree(`${work}/out`);
  const lock = pyodide.FS.analyzePath(`${work}/project/pylock.win_amd64.toml`).exists
    ? pyodide.FS.readFile(`${work}/project/pylock.win_amd64.toml`, { encoding: "utf8" })
    : null;
  post("done", { files: out, lock, seconds });
}

self.onmessage = (event) => {
  const msg = event.data;
  if (msg.type === "build") {
    build(msg).catch((err) => post("failed", { error: String(err && err.stack || err) }));
  }
};

ready = init().catch((err) => post("failed", { error: String(err && err.stack || err) }));
