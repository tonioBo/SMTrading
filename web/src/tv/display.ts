/**
 * The TV pages' own client: no portal session, no CSRF, no redirect to /login.
 * The display link arrives as `/tv#token=<link>`; the page trades it once for an HttpOnly
 * cookie (POST /api/tv-display/session, scoped to /api/tv-display) and then calls the
 * display routes with that cookie only.
 * Without a link (a TV that cannot be sent a URL, or a revoked link) the page asks for a
 * pairing code (POST /api/tv-display/code), shows it, and polls its own opaque id until a
 * user writes the code in the portal: that answer sets the same cookie.
 */
import { BUILD_HEADER } from "@/lib/buildVersion";
import type { TvPairCode, TvPairState, TvScreenKey, TvSelection } from "@/api/tvTypes";

export class TvFetchError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "TvFetchError";
    this.status = status;
  }
}

/** what the page does on its own: reload (a new build, the daily refresh); tests replace it */
export const tvEnv = {
  reload: () => window.location.reload(),
  ownBuild: (import.meta.env.VITE_BUILD_ID as string | undefined) ?? "",
};

/** the bundle is older than the server's: reload to pick up the new one */
let buildListener: (() => void) | null = null;
export function onNewBuild(cb: (() => void) | null): void {
  buildListener = cb;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init, headers: { Accept: "application/json", ...(init.headers ?? {}) } });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new TvFetchError(0, "server non raggiungibile");
  }
  const build = res.headers.get(BUILD_HEADER);
  if (res.ok && tvEnv.ownBuild && build && build !== tvEnv.ownBuild) buildListener?.();
  if (!res.ok) throw new TvFetchError(res.status, `HTTP ${res.status}`);
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function tvGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

/** trades the link token for the display cookie */
export function openSession(token: string, signal?: AbortSignal): Promise<unknown> {
  return request("/api/tv-display/session", {
    method: "POST",
    body: JSON.stringify({ token }),
    headers: { "Content-Type": "application/json" },
    signal,
  });
}

/** a new pairing code to show (no session needed) */
export function requestCode(signal?: AbortSignal): Promise<TvPairCode> {
  return request<TvPairCode>("/api/tv-display/code", { method: "POST", signal });
}

/** whether the code was written in the portal; `claimed` comes with the display cookie */
export function codeState(pollId: string, signal?: AbortSignal): Promise<{ state: TvPairState }> {
  return request(`/api/tv-display/code/${encodeURIComponent(pollId)}`, { signal });
}

/** how often the page asks about its code: the server's poll_s, kept within 1–5 s */
export const codePollMs = (c: Pick<TvPairCode, "poll_s"> | null) => Math.min(5, Math.max(1, c?.poll_s ?? 2)) * 1000;
/** too many codes asked from this address: wait this long before the next one */
export const CODE_LIMITED_MS = 60_000;

export const CODE_TEXT = "Apri il portale → Schermo TV → Abbina con codice e scrivi questo codice";

/** «123 456»: the code as shown */
export const spacedCode = (code: string) => (code.length === 6 ? `${code.slice(0, 3)} ${code.slice(3)}` : code);

/** the token of a `#token=…` fragment, or null */
export function tokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const t = params.get("token");
  return t && t.trim() ? t.trim() : null;
}

export const isRevoked = (err: unknown) => err instanceof TvFetchError && err.status === 401;

/** how often the selection is read: the server's poll_s, kept within 3–5 s */
export const pollMs = (s: TvSelection | null) => Math.min(5, Math.max(3, s?.poll_s ?? 4)) * 1000;

/** after failures (an api restart): 5, 10, 20, then every 30 s */
export const retryMs = (failures: number) => Math.min(30_000, 5_000 * 2 ** Math.max(0, failures - 1));

/** the page reloads itself once a day (only right after a good poll) */
export const DAILY_RELOAD_MS = 24 * 3600_000;

/** the screens shown in turn: the chosen one, or the rotation starting from it */
export function screenOrder(sel: Pick<TvSelection, "screen" | "rotation" | "screens">): TvScreenKey[] {
  if (!sel.rotation || sel.screens.length === 0) return [sel.screen];
  const i = sel.screens.indexOf(sel.screen);
  return i < 0 ? [...sel.screens] : [...sel.screens.slice(i), ...sel.screens.slice(0, i)];
}

export const SCREEN_LABELS: Record<TvScreenKey, string> = {
  flow: "Fiume dei flussi",
  campaign: "Campagna e portafogli",
  health: "Salute e avvisi",
};

export const REVOKED_TEXT = "collegamento revocato: apri di nuovo il link dal portale";
