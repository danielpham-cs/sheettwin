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
// `ask`: the chat shot's question when a language model is set up (its answer draws a chart); without one the shot
// uses the "find issues" suggestion, which the built-in rules answer.
const LABELS = {
  zh: { ask: "畫一張長條圖比較各產品的單位成本，用一句話說明重點", issues: "找出問題", graph: "依賴圖", charts: "圖表", exports: "轉換檔案", exportsReady: "SheetIR 資料庫" },
  en: { ask: "Draw a bar chart of each product's unit cost, and sum up the key point in one sentence", issues: "Find issues", graph: "Dependency graph", charts: "Charts", exports: "Converted files", exportsReady: "SheetIR database" },
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

const llm = await fetch(`${BASE}/api/llm`).then((r) => r.json(), () => null);
console.log(llm?.enabled ? `chat shot: asking ${llm.model} for a chart` : "chat shot: no language model, using the built-in rules");

const browser = await chromium.launch({ executablePath });
for (const [lang, L] of Object.entries(LABELS)) {
  mkdirSync(resolve(OUT, lang), { recursive: true });
  const ctx = await browser.newContext({ viewport: VIEWPORT });
  // Fresh chat history, fixed language, dark theme (the landing page shows one set of shots in both themes).
  await ctx.addInitScript((l) => {
    localStorage.setItem("sheettwin.lang", l);
    localStorage.setItem("sheettwin.theme", "dark");
    localStorage.removeItem("sheettwin.chats");
    localStorage.removeItem("sheettwin.project");
  }, lang);
  const page = await ctx.newPage();
  const save = async (name) => {
    await page.mouse.move(VIEWPORT.width - 5, VIEWPORT.height - 5); // no stray hover tooltips
    await page.waitForTimeout(400);
    await page.screenshot({ path: resolve(OUT, lang, `${name}.jpg`), type: "jpeg", quality: 85 });
    console.log(`  ${lang}/${name}.jpg`);
  };

  // A model words its answer differently each time: ask again (fresh chat) until it is a chart with no long table,
  // which fits the screen; keep the last try otherwise.
  for (let attempt = 1; attempt <= (llm?.enabled ? 3 : 1); attempt++) {
    if (attempt > 1) await page.evaluate(() => localStorage.removeItem("sheettwin.chats"));
    await page.goto(`${BASE}/app/`);
    if (llm?.enabled) {
      await page.locator("textarea").fill(L.ask);
      await page.keyboard.press("Enter");
    } else {
      await page.getByText(L.issues, { exact: true }).click();
    }
    // The answer is done when the chat log is no longer busy (its form depends on the model: a table, a list…).
    await page.locator('[role="log"][aria-busy="false"] .md').first().waitFor({ timeout: 240_000 });
    await page.waitForTimeout(1500);
    if (!llm?.enabled) break;
    const charted = await page.evaluate(() => !!document.querySelector('[role="log"] .recharts-wrapper') && !document.querySelector('[role="log"] .md table'));
    if (charted) break;
    console.log(`  ${lang}: answer ${attempt} has no chart or has a table, asking again`);
  }
  // Show the conversation from the question down, with no focus ring on the composer.
  await page.evaluate(() => {
    document.querySelector('[role="log"]').parentElement.scrollTop = 0;
    document.activeElement?.blur();
  });
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
