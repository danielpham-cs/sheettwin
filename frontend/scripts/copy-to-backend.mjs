// Copy the static export (out/) to sheetir/web/static/app, where `sheetir serve` picks it up.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const src = resolve(root, "out");
const dst = resolve(root, "../sheetir/web/static/app");

if (!existsSync(resolve(src, "index.html"))) {
  console.error(`no build found in ${src}; run "npm run build" first`);
  process.exit(1);
}
rmSync(dst, { recursive: true, force: true });
cpSync(src, dst, { recursive: true });
console.log(`copied ${src} -> ${dst}`);
