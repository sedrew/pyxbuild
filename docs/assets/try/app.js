// The "Try in the browser" page: a small file editor around worker.js.
"use strict";

(function () {
  const root = document.getElementById("pyx-try");
  if (!root) return;
  const SCRIPT_URL = document.currentScript ? document.currentScript.src : location.href;

  const EXAMPLE = {
    "pyproject.toml": `[project]
name = "hello-web"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["rich"]

[project.scripts]
hello-web = "hello_web:main"

[tool.pyxbuild]
python = "3.12"
`,
    "src/hello_web/__init__.py": `from rich.console import Console
from rich.table import Table


def main():
    console = Console()
    table = Table(title="Built in the browser with pyxbuild")
    table.add_column("What")
    table.add_column("Value")
    table.add_row("Python", __import__("sys").version.split()[0])
    table.add_row("Platform", __import__("platform").platform())
    console.print(table)
    input("Press Enter to close...")
`,
  };

  const $ = (sel) => root.querySelector(sel);
  const tabs = $(".pyx-tabs");
  const editor = $(".pyx-editor");
  const log = $(".pyx-log");
  const buildBtn = $(".pyx-build");
  const result = $(".pyx-result");
  const status = $(".pyx-status");
  let files = { ...EXAMPLE };
  let current = "pyproject.toml";
  let worker = null;
  let busy = false;

  function renderTabs() {
    tabs.innerHTML = "";
    for (const name of Object.keys(files).sort((a, b) =>
      a === "pyproject.toml" ? -1 : b === "pyproject.toml" ? 1 : a.localeCompare(b))) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pyx-tab" + (name === current ? " is-active" : "");
      b.textContent = name;
      b.title = name;
      b.onclick = () => select(name);
      if (name !== "pyproject.toml") {
        const x = document.createElement("span");
        x.className = "pyx-tab-x";
        x.textContent = "×";
        x.title = "Remove file";
        x.onclick = (ev) => {
          ev.stopPropagation();
          delete files[name];
          if (current === name) current = "pyproject.toml";
          renderTabs();
          editor.value = files[current];
        };
        b.appendChild(x);
      }
      tabs.appendChild(b);
    }
  }

  function select(name) {
    files[current] = editor.value;
    current = name;
    editor.value = files[name];
    renderTabs();
  }

  $(".pyx-add").onclick = () => {
    const name = prompt("File path inside the project, e.g. src/hello_web/util.py");
    if (!name) return;
    const clean = name.replace(/^\/+/, "").replace(/\\/g, "/");
    if (!files[clean]) files[clean] = "";
    select(clean);
  };

  $(".pyx-open").onchange = async (ev) => {
    const picked = Array.from(ev.target.files || []);
    if (!picked.length) return;
    const next = {};
    const strip = picked[0].webkitRelativePath.split("/")[0] + "/";
    for (const f of picked) {
      const rel = (f.webkitRelativePath || f.name).replace(strip, "");
      if (/(^|\/)(\.git|\.venv|__pycache__|dist|node_modules)(\/|$)/.test(rel)) continue;
      if (f.size > 2 * 1024 * 1024) continue;
      next[rel] = await f.text();
    }
    if (!next["pyproject.toml"]) {
      alert("The folder has no pyproject.toml at its top level.");
      return;
    }
    files = next;
    current = "pyproject.toml";
    editor.value = files[current];
    renderTabs();
  };

  $(".pyx-reset").onclick = () => {
    files = { ...EXAMPLE };
    current = "pyproject.toml";
    editor.value = files[current];
    renderTabs();
  };

  function append(line) {
    log.textContent += line + "\n";
    log.scrollTop = log.scrollHeight;
  }

  function setStatus(text, kind) {
    status.textContent = text;
    status.dataset.kind = kind || "";
  }

  function startWorker() {
    if (worker) return worker;
    worker = new Worker(new URL("worker.js", SCRIPT_URL));
    worker.onmessage = (ev) => {
      const m = ev.data;
      if (m.type === "log") append(m.line);
      else if (m.type === "ready") {
        append(`pyxbuild ${m.version} is ready.`);
        setStatus("Ready", "ok");
      } else if (m.type === "done") finish(m);
      else if (m.type === "failed") {
        if (m.error) append(m.error);
        setStatus(`Build failed${m.seconds ? ` after ${m.seconds} s` : ""}`, "error");
        busy = false;
        buildBtn.disabled = false;
      }
    };
    return worker;
  }

  function human(n) {
    return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`;
  }

  function finish(m) {
    busy = false;
    buildBtn.disabled = false;
    setStatus(`Built in ${m.seconds} s`, "ok");
    result.innerHTML = "";
    const outputs = Object.entries(m.files).filter(([name]) => /\.(exe|zip)$/i.test(name));
    for (const [name, data] of outputs) {
      const a = document.createElement("a");
      a.className = "md-button md-button--primary pyx-download";
      a.href = URL.createObjectURL(new Blob([data], { type: "application/octet-stream" }));
      a.download = name.split("/").pop();
      a.textContent = `Download ${a.download} (${human(data.length)})`;
      result.appendChild(a);
    }
    if (m.lock) {
      const a = document.createElement("a");
      a.className = "md-button pyx-download";
      a.href = URL.createObjectURL(new Blob([m.lock], { type: "text/plain" }));
      a.download = "pylock.win_amd64.toml";
      a.textContent = "Lock file";
      a.title = "Commit it next to pyproject.toml for the same versions everywhere";
      result.appendChild(a);
    }
    result.hidden = false;
  }

  buildBtn.onclick = () => {
    if (busy) return;
    files[current] = editor.value;
    busy = true;
    buildBtn.disabled = true;
    result.hidden = true;
    log.textContent = "";
    setStatus("Building…", "busy");
    const mode = $(".pyx-mode").value;
    const args = mode === "onefile" ? ["--mode", "onefile"] : ["--mode", "folder", "--zip"];
    const python = $(".pyx-python").value;
    if (python) args.push("--python", python);
    const enc = new TextEncoder();
    const payload = {};
    for (const [k, v] of Object.entries(files)) payload[k] = enc.encode(v);
    startWorker().postMessage({ type: "build", files: payload, args });
  };

  editor.addEventListener("keydown", (ev) => {
    if (ev.key === "Tab") {
      ev.preventDefault();
      const s = editor.selectionStart;
      editor.setRangeText("    ", s, editor.selectionEnd, "end");
    }
  });

  editor.value = files[current];
  renderTabs();
  setStatus("Python loads on the first build (≈10 MB, then cached by the browser)", "");
})();
