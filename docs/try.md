---
title: Try in the browser
hide:
  - navigation
  - toc
---

# Try pyxbuild in the browser

Edit a small project, press **Build** and download a Windows `.exe`. The real
pyxbuild runs on your device in [Pyodide](https://pyodide.org) (Python
compiled to WebAssembly): dependencies are resolved against PyPI, wheels and
the Windows Python runtime are downloaded by your browser, nothing is sent to
a build server. Works on Linux, macOS, ChromeOS, Android and iPad.

<div id="pyx-try" class="pyx-try">
  <div class="pyx-toolbar">
    <div class="pyx-tabs" role="tablist"></div>
    <button type="button" class="pyx-add md-button" title="Add a file">+ File</button>
    <label class="md-button pyx-open-label" title="Open a project folder">
      Open folder<input type="file" class="pyx-open" webkitdirectory multiple hidden>
    </label>
    <button type="button" class="pyx-reset md-button" title="Back to the example">Example</button>
  </div>
  <textarea class="pyx-editor" spellcheck="false" aria-label="File contents"></textarea>
  <div class="pyx-controls">
    <label>Output
      <select class="pyx-mode">
        <option value="onefile" selected>One .exe</option>
        <option value="folder">Folder (.zip)</option>
      </select>
    </label>
    <label>Python
      <select class="pyx-python">
        <option value="">from pyproject</option>
        <option>3.11</option>
        <option>3.12</option>
        <option>3.13</option>
        <option>3.14</option>
      </select>
    </label>
    <button type="button" class="pyx-build md-button md-button--primary">Build</button>
    <span class="pyx-status" aria-live="polite"></span>
  </div>
  <div class="pyx-result" hidden></div>
  <pre class="pyx-log" aria-label="Build log"></pre>
</div>

<script src="../assets/try/app.js"></script>

**What works here:** pure-Python projects (`src/<package>/`, `<package>/` or
`<module>.py` named after the project, static `[project]` metadata) with
dependencies that have Windows wheels on PyPI. For local packages,
monorepos, sdists, private indexes, code signing and CI, use the
[command line](index.md#usage) or the [GitHub Action](index.md#github-actions).
