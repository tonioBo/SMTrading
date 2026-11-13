/**
 * fetch wrapper for the admin API: session cookies, CSRF header on writes,
 * Idempotency-Key, `ApiError` from the error JSON, one redirect to /login on 401.
 */

export const CSRF_COOKIE = "smt_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiFetchOptions {
  method?: HttpMethod;
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

interface ClientConfig {
  /** Navigates to the login URL; the default does a full page load. */
  onUnauthorized: (loginUrl: string) => void;
}

const config: ClientConfig = {
  onUnauthorized: (url) => window.location.assign(url),
};

let redirectedToLogin = false;

export function configureClient(overrides: Partial<ClientConfig>): void {
  Object.assign(config, overrides);
}

/** Call after a successful login: the next 401 redirects again. */
export function resetUnauthorized(): void {
  redirectedToLogin = false;
}

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

/** Redirects to /login?next=<current page>, once until resetUnauthorized(). */
export function notifyUnauthorized(): void {
  const { pathname, search } = window.location;
  if (redirectedToLogin || pathname === "/login") return;
  redirectedToLogin = true;
  config.onUnauthorized(`/login?next=${encodeURIComponent(pathname + search)}`);
}

async function toApiError(res: Response): Promise<ApiError> {
  try {
    const data = (await res.json()) as { code?: unknown; message?: unknown; details?: unknown };
    if (typeof data?.code === "string" && typeof data?.message === "string") {
      return new ApiError(res.status, data.code, data.message, data.details ?? null);
    }
  } catch {
    // not JSON: fall through to a generic error
  }
  return new ApiError(res.status, "HTTP_ERROR", `errore del server (HTTP ${res.status})`);
}

/** The fetch of apiFetch/apiDownload: an ok Response, else ApiError (401 → login). */
async function apiRequest(path: string, options: ApiFetchOptions, accept: string): Promise<Response> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: accept };
  if (method !== "GET") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers[CSRF_HEADER] = csrf;
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, credentials: "include", signal: options.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "NETWORK_ERROR", "impossibile contattare il server");
  }

  if (!res.ok) {
    if (res.status === 401) notifyUnauthorized();
    throw await toApiError(res);
  }
  return res;
}

export async function apiFetch<T = unknown>(path: string, options: ApiFetchOptions = {}): Promise<T> {
  const res = await apiRequest(path, options, "application/json");
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export interface Download {
  blob: Blob;
  /** from Content-Disposition, else `fallbackName` */
  filename: string;
}

/** A file response (e.g. POST /api/system/backup) read as a Blob. */
export async function apiDownload(path: string, fallbackName: string, options: ApiFetchOptions = {}): Promise<Download> {
  const res = await apiRequest(path, options, "application/octet-stream, application/json");
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? fallbackName };
}

/** Hands a Blob to the browser as a file download. */
export function saveBlob({ blob, filename }: Download): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
