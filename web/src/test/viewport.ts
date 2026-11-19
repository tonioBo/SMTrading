/**
 * Emulates a viewport width in jsdom: `window.innerWidth` and a `matchMedia` that evaluates
 * `(max-width: Npx)` / `(min-width: Npx)` against it (other queries never match). Call
 * `resetViewport()` after the test.
 */
import { act } from "@testing-library/react";

type Listener = (e: MediaQueryListEvent) => void;
const lists = new Set<{ query: string; listeners: Set<Listener> }>();
const original = { matchMedia: window.matchMedia, innerWidth: window.innerWidth };
let width = original.innerWidth;

function evaluate(query: string): boolean {
  const parts = query.match(/\((min|max)-width:\s*(\d+)px\)/g);
  if (!parts) return false;
  return parts.every((p) => {
    const [, kind, px] = p.match(/\((min|max)-width:\s*(\d+)px\)/)!;
    return kind === "max" ? width <= Number(px) : width >= Number(px);
  });
}

function fakeMatchMedia(query: string): MediaQueryList {
  const entry = { query, listeners: new Set<Listener>() };
  lists.add(entry);
  return {
    get matches() {
      return evaluate(query);
    },
    media: query,
    onchange: null,
    addEventListener: (_: string, l: Listener) => entry.listeners.add(l),
    removeEventListener: (_: string, l: Listener) => entry.listeners.delete(l),
    addListener: (l: Listener) => entry.listeners.add(l),
    removeListener: (l: Listener) => entry.listeners.delete(l),
    dispatchEvent: () => true,
  } as unknown as MediaQueryList;
}

export function setViewportWidth(px: number): void {
  width = px;
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
  window.matchMedia = fakeMatchMedia;
  act(() => {
    lists.forEach((e) => e.listeners.forEach((l) => l({ matches: evaluate(e.query), media: e.query } as MediaQueryListEvent)));
  });
}

export function resetViewport(): void {
  lists.clear();
  width = original.innerWidth;
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: original.innerWidth });
  window.matchMedia = original.matchMedia;
}
