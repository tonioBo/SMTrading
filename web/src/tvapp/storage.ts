/** The app's memory on the TV (localStorage): the server address and the display token.
 * Nothing secret ships in the package: the token arrives with the pairing code. */

export const BASE_KEY = "smt.tvapp.base";
export const TOKEN_KEY = "smt.tvapp.token";

/** the address proposed at the first start: the app's http entry on the SMTrading PC
 * (build-time override: VITE_TV_APP_BASE) */
export const DEFAULT_BASE =
  ((import.meta.env.VITE_TV_APP_BASE as string | undefined) || "").trim() || "http://192.168.30.7:8087";

type Store = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function store(): Store | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/** «192.168.30.7:8087», «http://host:8087/» → «http://host:8087»; null when not an address */
export function normalizeBase(raw: string | null | undefined): string | null {
  let text = (raw ?? "").trim();
  if (!text) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `http://${text}`;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (!url.hostname || url.username || url.password || url.search || url.hash) return null;
  if (url.pathname !== "/" && url.pathname !== "") return null;
  return url.origin;
}

export function loadStored(s: Store | null = store()): { base: string | null; token: string | null } {
  try {
    const base = normalizeBase(s?.getItem(BASE_KEY));
    const token = s?.getItem(TOKEN_KEY) || null;
    return { base, token };
  } catch {
    return { base: null, token: null };
  }
}

export function saveStored(v: { base: string | null; token: string | null }, s: Store | null = store()): void {
  try {
    if (v.base) s?.setItem(BASE_KEY, v.base);
    else s?.removeItem(BASE_KEY);
    if (v.token) s?.setItem(TOKEN_KEY, v.token);
    else s?.removeItem(TOKEN_KEY);
  } catch {
    // storage full or blocked: the app keeps working until it is closed
  }
}
