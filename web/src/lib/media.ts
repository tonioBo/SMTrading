/**
 * Media queries for the phone layout. Without `matchMedia` (jsdom, old browsers) nothing
 * matches: the desktop layout. Tests stub `window.matchMedia` to emulate a width.
 */
import { useSyncExternalStore } from "react";

/** Below Tailwind's `sm` breakpoint (640px): tables become stacked cards. */
export const NARROW_QUERY = "(max-width: 639px)";

function mql(query: string): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = mql(query);
      if (!m) return () => {};
      m.addEventListener?.("change", cb);
      return () => m.removeEventListener?.("change", cb);
    },
    () => mql(query)?.matches ?? false,
    () => false,
  );
}

export function useIsNarrow(): boolean {
  return useMediaQuery(NARROW_QUERY);
}
