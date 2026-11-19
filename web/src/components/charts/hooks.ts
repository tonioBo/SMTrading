/** Hooks shared by the chart components (no chart library here). */
import { useEffect, useMemo, type RefObject } from "react";
import { readPalette, type Palette } from "@/lib/chartTheme";
import { useTheme } from "@/theme";

/** The theme's colours for a canvas, read again when the theme changes. */
export function usePalette(): Palette {
  const { resolved } = useTheme();
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `resolved` is the trigger: the tokens live in CSS
  return useMemo(() => readPalette(), [resolved]);
}

/**
 * Calls `onResize(width, height)` whenever the element changes size (ResizeObserver; once
 * on mount where there is none, e.g. jsdom), until unmount.
 */
export function useResize(ref: RefObject<HTMLElement>, onResize: (width: number, height: number) => void, ready: unknown): void {
  useEffect(() => {
    const el = ref.current;
    if (!el || !ready) return;
    if (typeof ResizeObserver === "undefined") {
      onResize(el.clientWidth, el.clientHeight);
      return;
    }
    const ro = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box && box.width > 0) onResize(Math.floor(box.width), Math.floor(box.height));
    });
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-subscribed per chart instance only
  }, [ref, ready]);
}
