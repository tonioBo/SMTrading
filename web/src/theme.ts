/** Theme: dark / light / auto (prefers-color-scheme), the `dark` class on <html>,
 * persisted in localStorage (dark by default). public/theme-init.js applies it before the
 * first paint with the same rules. */
import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light" | "auto";
export type ResolvedTheme = "dark" | "light";

export const THEME_KEY = "smt-theme";
export const THEME_LABELS: Record<Theme, string> = { dark: "scuro", light: "chiaro", auto: "automatico" };
const ORDER: Theme[] = ["dark", "light", "auto"];
const DARK_QUERY = "(prefers-color-scheme: dark)";
const listeners = new Set<() => void>();

export function readStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "auto" ? v : "dark";
  } catch {
    return "dark"; // storage blocked
  }
}

function systemQuery(): MediaQueryList | null {
  try {
    return typeof window.matchMedia === "function" ? window.matchMedia(DARK_QUERY) : null;
  } catch {
    return null;
  }
}

export function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme !== "auto") return theme;
  return systemQuery()?.matches === false ? "light" : "dark";
}

export function nextTheme(theme: Theme): Theme {
  return ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
}

let current: Theme = readStoredTheme();
let resolved: ResolvedTheme = resolveTheme(current);
let watched: MediaQueryList | null = null;

function onSystemChange(): void {
  if (current !== "auto") return;
  apply();
  listeners.forEach((l) => l());
}

function apply(): void {
  resolved = resolveTheme(current);
  document.documentElement.classList.toggle("dark", resolved === "dark");
  // automatic: follow the system while it changes (subscribed once)
  if (current === "auto" && watched == null) {
    watched = systemQuery();
    watched?.addEventListener?.("change", onSystemChange);
  } else if (current !== "auto" && watched != null) {
    watched.removeEventListener?.("change", onSystemChange);
    watched = null;
  }
}

export function initTheme(): void {
  current = readStoredTheme();
  apply();
}

export function setTheme(theme: Theme): void {
  current = theme;
  apply();
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // storage unavailable (private mode): the choice lasts for this page only
  }
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useTheme(): { theme: Theme; resolved: ResolvedTheme; cycle: () => void } {
  const theme = useSyncExternalStore(subscribe, () => current);
  const res = useSyncExternalStore(subscribe, () => resolved);
  return { theme, resolved: res, cycle: () => setTheme(nextTheme(theme)) };
}
