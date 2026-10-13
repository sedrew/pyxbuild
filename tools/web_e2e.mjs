// End-to-end test of the browser sandbox (docs/try.md) in headless Chromium.
//
//   node tools/web_e2e.mjs http://127.0.0.1:8765/ OUT.exe
//
// Opens the "Try" page served by tools/web_mirror.py, points the example
// project at the recorded index (/simple/), presses Build and saves the
// downloaded executable. Needs playwright-core (npm) and a Chromium; set
// CHROMIUM to its executable if Playwright's own is not installed.
import { writeFileSync } from "node:fs";
import { chromium } from "playwright-core";

const [base, out] = process.argv.slice(2);
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const page = await browser.newPage({ acceptDownloads: true });
page.on("console", (m) => console.log(`[page] ${m.text()}`));
page.on("pageerror", (e) => console.log(`[page error] ${e}`));
await page.goto(new URL("try/", base).href);
await page.waitForSelector(".pyx-editor");

// use the recorded index instead of PyPI
await page.evaluate((index) => {
  const editor = document.querySelector(".pyx-editor");
  editor.value = editor.value.replace("[tool.pyxbuild]\n",
    `[tool.pyxbuild]\nindex-url = "${index}"\n`);
}, new URL("simple", base).href);

const started = Date.now();
await page.click(".pyx-build");
const status = page.locator(".pyx-status");
await page.waitForFunction(() => {
  const s = document.querySelector(".pyx-status").dataset.kind;
  return s === "ok" && document.querySelector(".pyx-result:not([hidden]) a") || s === "error";
}, null, { timeout: 15 * 60 * 1000, polling: 1000 });
const log = await page.locator(".pyx-log").textContent();
console.log(log);
console.log(`status: ${await status.textContent()} (${((Date.now() - started) / 1000).toFixed(1)} s)`);
if ((await status.getAttribute("data-kind")) !== "ok") {
  await browser.close();
  process.exit(1);
}
const [download] = await Promise.all([
  page.waitForEvent("download"),
  page.locator(".pyx-result a").first().click(),
]);
await download.saveAs(out);
const lock = await page.locator(".pyx-result a", { hasText: "Lock file" }).count();
console.log(`saved ${download.suggestedFilename()} -> ${out}; lock link: ${lock}`);
writeFileSync(`${out}.log`, log);
await browser.close();
