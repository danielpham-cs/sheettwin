"use client";

import { useSyncExternalStore } from "react";
import { THEME_STORE_KEY as STORE_KEY } from "@/lib/theme-script";

// Light / dark theme. The choice lives on <html data-theme> (globals.css remaps the palette from it) and in
// localStorage; without a saved choice the OS preference decides (lib/theme-script.ts, before first paint).
export type Theme = "light" | "dark";

const listeners = new Set<() => void>();

function current(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function setTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORE_KEY, theme);
  } catch {}
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTheme(): [Theme, () => void] {
  const theme = useSyncExternalStore(subscribe, current, () => "dark" as Theme);
  return [theme, () => setTheme(theme === "light" ? "dark" : "light")];
}

// Chart colors for Recharts / React Flow props (SVG attributes, so not themable from CSS). Categorical slots follow
// the dataviz reference palette's light and dark steps (same hue order); ink and grid are its text tokens.
type Ink = Record<"primary" | "secondary" | "muted" | "grid" | "axis" | "surface" | "popover" | "cursor" | "border", string>;
const PALETTES: Record<Theme, { CAT: readonly string[]; INK: Ink }> = {
  dark: {
    CAT: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
    INK: {
      primary: "#ffffff", secondary: "#c3c2b7", muted: "#898781", grid: "#2c2c2a", axis: "#383835",
      surface: "#15161a", popover: "#15161a", cursor: "rgba(255,255,255,0.04)", border: "rgba(255,255,255,0.1)",
    },
  },
  light: {
    CAT: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    INK: {
      primary: "#0b0b0b", secondary: "#52514e", muted: "#898781", grid: "#e8e8e4", axis: "#c3c2b7",
      surface: "#ffffff", popover: "#ffffff", cursor: "rgba(0,0,0,0.04)", border: "rgba(0,0,0,0.1)",
    },
  },
};

export function useChartPalette() {
  return PALETTES[useTheme()[0]];
}
