import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { initTheme, nextTheme, readStoredTheme, resolveTheme, setTheme, THEME_KEY } from "@/theme";

function mockSystemDark(dark: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    get matches() {
      return dark;
    },
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => mql));
  return {
    set(next: boolean) {
      dark = next;
      listeners.forEach((l) => l());
    },
  };
}

const isDark = () => document.documentElement.classList.contains("dark");

describe("theme", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("reads the stored choice: dark by default, light, auto; anything else is dark", () => {
    expect(readStoredTheme()).toBe("dark");
    localStorage.setItem(THEME_KEY, "light");
    expect(readStoredTheme()).toBe("light");
    localStorage.setItem(THEME_KEY, "auto");
    expect(readStoredTheme()).toBe("auto");
    localStorage.setItem(THEME_KEY, "blue");
    expect(readStoredTheme()).toBe("dark");
  });

  it("blocked storage: dark, and setTheme still applies without throwing", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readStoredTheme()).toBe("dark");
    expect(() => setTheme("light")).not.toThrow();
    expect(isDark()).toBe(false);
    setTheme("dark");
  });

  it("auto follows prefers-color-scheme, live", () => {
    const system = mockSystemDark(false);
    expect(resolveTheme("auto")).toBe("light");
    setTheme("auto");
    expect(localStorage.getItem(THEME_KEY)).toBe("auto");
    expect(isDark()).toBe(false);
    system.set(true);
    expect(isDark()).toBe(true);
    setTheme("dark");
    system.set(false); // no longer automatic: the change is ignored
    expect(isDark()).toBe(true);
  });

  it("initTheme applies the stored choice", () => {
    mockSystemDark(true);
    localStorage.setItem(THEME_KEY, "light");
    initTheme();
    expect(isDark()).toBe(false);
    localStorage.setItem(THEME_KEY, "auto");
    initTheme();
    expect(isDark()).toBe(true);
    setTheme("dark");
  });

  it("cycles scuro → chiaro → automatico → scuro", () => {
    expect(nextTheme("dark")).toBe("light");
    expect(nextTheme("light")).toBe("auto");
    expect(nextTheme("auto")).toBe("dark");
  });
});
