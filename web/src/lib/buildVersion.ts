/**
 * Stale bundle detection: every API response names the deployed build (X-SMT-Build); a
 * tab whose bundle has another id (VITE_BUILD_ID, embedded at build time) offers a reload.
 * Without a build id (dev server, tests) nothing is compared.
 */
import { useSyncExternalStore } from "react";

export const BUILD_HEADER = "X-SMT-Build";

let ownBuild: string = import.meta.env.VITE_BUILD_ID ?? "";
let outdated = false;
const listeners = new Set<() => void>();

/** Tests only (before rendering): pretend the bundle was built with `id`. */
export function setOwnBuild(id: string): void {
  ownBuild = id;
  outdated = false;
}

/** Called by the API client with the header of every response. */
export function noteServerBuild(serverBuild: string | null): void {
  if (!ownBuild || !serverBuild || outdated || serverBuild === ownBuild) return;
  outdated = true;
  listeners.forEach((l) => l());
}

export function useNewVersionAvailable(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => outdated,
  );
}
