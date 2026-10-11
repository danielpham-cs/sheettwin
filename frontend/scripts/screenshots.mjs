// Re-shoot the app screenshots used by the landing page (public/screenshots/{zh,en}/*.jpg).
//
//   1. npm run build                                   (the app must be served at /app/)
//   2. sheetir serve --data <an empty folder>          (so the sidebar shows only the sample)
//   3. npm run screenshots [-- http://127.0.0.1:8765]
//
// Uses an installed Edge or Chrome; set SHEETTWIN_BROWSER to the executable to choose another.
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const BASE = (process.argv[2] ?? process.env.SHEETIR_URL ?? "http://127.0.0.1:8765").replace(/\/$/, "");
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "../public/screenshots");
const VIEWPORT = { width: 1440, height: 900 };

// UI labels the script clicks, per language (they mirror src/lib/i18n.tsx).
const LABELS = {
  zh: { issues: "找出問題", graph: "依賴圖", charts: "圖表", exports: "轉換檔案", exportsReady: "SheetIR 資料庫" },
  en: { issues: "Find issues", graph: "Dependency graph", charts: "Charts", exports: "Converted files", exportsReady: "SheetIR database" },
};

const BROWSERS = [
  process.env.SHEETTWIN_BROWSER,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);
const executablePath = BROWSERS.find((p) => existsSync(p));
if (!executablePath) {
  console.error("No Edge/Chrome found; set SHEETTWIN_BROWSER to a Chromium-based browser executable.");
  process.exit(1);
}

const res = await fetch(`${BASE}/api/workbooks/sample`, { method: "POST" }).catch(() => null);
if (!res?.ok) {
  console.error(`Could not load the sample workbook from ${BASE}. Is "sheetir serve" running?`);
  process.exit(1);
}

const browser = await chromium.launch({ executablePath });
for (const [lang, L] of Object.entries(LABELS)) {
  mkdirSync(resolve(OUT, lang), { recursive: true });
  const ctx = await browser.newContext({ viewport: VIEWPORT });
  // Fresh chat history, fixed language.
  await ctx.addInitScript((l) => {
    localStorage.setItem("sheettwin.lang", l);
    localStorage.removeItem("sheettwin.chats");
  }, lang);
  const page = await ctx.newPage();
  const save = async (name) => {
    await page.mouse.move(VIEWPORT.width - 5, VIEWPORT.height - 5); // no stray hover tooltips
    await page.waitForTimeout(400);
    await page.screenshot({ path: resolve(OUT, lang, `${name}.jpg`), type: "jpeg", quality: 85 });
    console.log(`  ${lang}/${name}.jpg`);
  };

  await page.goto(`${BASE}/app/`);
  await page.getByText(L.issues, { exact: true }).click();
  await page.locator(".md table").first().waitFor();
  await page.waitForTimeout(1200);
  await save("chat");

  await page.getByLabel(L.graph, { exact: true }).click();
  await page.locator(".react-flow__node").first().waitFor();
  await page.waitForTimeout(1500);
  const node = page.locator(".react-flow__node", { hasText: "quote[*].unit_cost" }).first();
  await node.hover();
  await page.waitForTimeout(600);
  await page.screenshot({ path: resolve(OUT, lang, "graph.jpg"), type: "jpeg", quality: 85 }); // keep the hover highlight
  console.log(`  ${lang}/graph.jpg`);

  await page.getByLabel(L.charts, { exact: true }).click();
  await page.locator(".recharts-pie").first().waitFor();
  await page.waitForTimeout(1500);
  await save("charts");

  await page.getByLabel(L.exports, { exact: true }).click();
  await page.getByText(L.exportsReady).first().waitFor();
  await page.waitForTimeout(800);
  await save("exports");

  await ctx.close();
}
await browser.close();
console.log(`saved to ${OUT}`);
