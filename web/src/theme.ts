/** Dark/light theme: `dark` class on <html>, persisted in localStorage (dark by default). */
import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";

export const THEME_KEY = "smt-theme";
const listeners = new Set<() => void>();

function readStored(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

let current: Theme = readStored();

function apply(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function initTheme(): void {
  current = readStored();
  apply(current);
}

export function setTheme(theme: Theme): void {
  current = theme;
  apply(theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // storage unavailable (private mode): the choice lasts for this page only
  }
  listeners.forEach((l) => l());
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => current,
  );
  return { theme, toggle: () => setTheme(theme === "dark" ? "light" : "dark") };
}
