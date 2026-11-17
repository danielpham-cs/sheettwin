// Runs in <head> before first paint (app/layout.tsx), so the page never flashes the other theme. Kept out of
// lib/theme.ts: that module is client-only, and a server component cannot read a client module's constants.
export const THEME_STORE_KEY = "sheettwin.theme";

export const THEME_SCRIPT = `try{var t=localStorage.getItem("${THEME_STORE_KEY}");if(t!=="light"&&t!=="dark")t=matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";document.documentElement.dataset.theme=t}catch(e){document.documentElement.dataset.theme="dark"}`;
