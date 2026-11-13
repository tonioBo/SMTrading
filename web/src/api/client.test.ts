import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiFetch, configureClient, resetUnauthorized } from "./client";

type Seen = { method: string; csrf: string | null; idem: string | null; body: unknown };
const seen: Seen[] = [];

const server = setupServer(
  http.all("*/api/echo", async ({ request }) => {
    const text = await request.text();
    seen.push({
      method: request.method,
      csrf: request.headers.get("X-CSRF-Token"),
      idem: request.headers.get("Idempotency-Key"),
      body: text ? JSON.parse(text) : null,
    });
    return HttpResponse.json({ ok: true });
  }),
  http.get("*/api/private", () =>
    HttpResponse.json({ code: "FORBIDDEN", message: "login richiesto", details: null }, { status: 401 }),
  ),
  http.get("*/api/missing", () =>
    HttpResponse.json({ code: "NOT_FOUND", message: "non trovato", details: { id: "x" } }, { status: 404 }),
  ),
  http.delete("*/api/nocontent", () => new HttpResponse(null, { status: 204 })),
);

const onUnauthorized = vi.fn();

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  seen.length = 0;
  onUnauthorized.mockReset();
  configureClient({ onUnauthorized });
  resetUnauthorized();
  document.cookie = "smt_csrf=tok123; path=/";
  window.history.replaceState(null, "", "/portafogli?tab=1");
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("apiFetch", () => {
  it("does not send the CSRF header on GET", async () => {
    await expect(apiFetch("/api/echo")).resolves.toEqual({ ok: true });
    expect(seen[0]).toMatchObject({ method: "GET", csrf: null });
  });

  it("sends the CSRF header from the smt_csrf cookie on non-GET methods", async () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE"] as const) {
      await apiFetch("/api/echo", { method, body: { a: "1.50" } });
    }
    expect(seen.map((s) => s.csrf)).toEqual(["tok123", "tok123", "tok123", "tok123"]);
    expect(seen[0].body).toEqual({ a: "1.50" });
  });

  it("sends Idempotency-Key only when given", async () => {
    await apiFetch("/api/echo", { method: "POST", body: {}, idempotencyKey: "k-1" });
    await apiFetch("/api/echo", { method: "POST", body: {} });
    expect(seen.map((s) => s.idem)).toEqual(["k-1", null]);
  });

  it("raises ApiError with the error JSON fields", async () => {
    const err = await apiFetch("/api/missing").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "NOT_FOUND", message: "non trovato", details: { id: "x" } });
  });

  it("returns undefined for 204", async () => {
    await expect(apiFetch("/api/nocontent", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("redirects to /login with next only once on repeated 401", async () => {
    await expect(apiFetch("/api/private")).rejects.toMatchObject({ status: 401 });
    await expect(apiFetch("/api/private")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(onUnauthorized).toHaveBeenCalledWith("/login?next=%2Fportafogli%3Ftab%3D1");
  });

  it("redirects again after resetUnauthorized (successful login)", async () => {
    await apiFetch("/api/private").catch(() => undefined);
    resetUnauthorized();
    await apiFetch("/api/private").catch(() => undefined);
    expect(onUnauthorized).toHaveBeenCalledTimes(2);
  });

  it("does not redirect while already on the login page", async () => {
    window.history.replaceState(null, "", "/login");
    await apiFetch("/api/private").catch(() => undefined);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
