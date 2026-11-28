import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { TvSelection } from "@/api/tvTypes";
import { tvTransport } from "@/tv/display";
import TvApp from "@/tvapp/TvApp";
import { BASE_KEY, TOKEN_KEY } from "@/tvapp/storage";

vi.mock("@/tv/sounds", () => ({
  TvSounds: class {
    configure() {}
    baseline() {}
    handle() {}
    blocked() {
      return false;
    }
    onChange() {
      return () => {};
    }
    unlock() {}
    destroy() {}
  },
}));
vi.mock("@/tv/screens", () => ({
  CampaignScreen: () => <div data-testid="screen">campaign</div>,
  HealthScreen: () => <div data-testid="screen">health</div>,
}));
vi.mock("@/tv/FlowScreen", () => ({ FlowScreen: () => <div data-testid="screen">flow</div> }));

const BASE = "http://192.168.30.7:8087";
const SOUND = { enabled: false, volume: 40, quiet_start: "22:00", quiet_end: "08:00", tz: "Europe/Rome", quiet_now: false };
const sel = (over: Partial<TvSelection> = {}): TvSelection => ({
  screen: "campaign",
  rotation: false,
  interval_s: 30,
  version: 1,
  updated_at: "2026-11-28T10:00:00Z",
  screens: ["flow", "campaign", "health"],
  poll_s: 4,
  now: "2026-11-28T10:00:00Z",
  sound: SOUND,
  sound_events: [],
  sound_cursor: "2026-11-28T10:00:00Z",
  ...over,
});

let selectionStatus = 200;
let claim: { state: string; token?: string } = { state: "pending" };
const seen: { method: string; url: string; headers: Record<string, string> }[] = [];
const record = (request: Request) =>
  seen.push({ method: request.method, url: request.url, headers: Object.fromEntries(request.headers.entries()) });
const server = setupServer(
  http.get(`${BASE}/tv-app/health`, () => HttpResponse.json({ ok: true })),
  http.post(`${BASE}/api/tv-display/code`, ({ request }) => {
    record(request);
    return HttpResponse.json({ poll_id: "p".repeat(32), code: "123456", expires_in_s: 600, poll_s: 1 });
  }),
  http.get(`${BASE}/api/tv-display/code/:id`, ({ request }) => {
    record(request);
    return HttpResponse.json(claim);
  }),
  http.get(`${BASE}/api/tv-display/selection`, ({ request }) => {
    record(request);
    if (selectionStatus !== 200) return HttpResponse.json({ code: "FORBIDDEN", message: "revocato" }, { status: selectionStatus });
    return HttpResponse.json(sel());
  }),
);

const exit = vi.fn();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  localStorage.clear();
  seen.length = 0;
  selectionStatus = 200;
  claim = { state: "pending" };
  exit.mockReset();
  (globalThis as { tizen?: unknown }).tizen = { application: { getCurrentApplication: () => ({ exit }) }, tvinputdevice: { registerKey: () => {} } };
});
afterEach(() => {
  server.resetHandlers();
  delete (globalThis as { tizen?: unknown }).tizen;
  tvTransport.base = "";
  tvTransport.credentials = "same-origin";
  tvTransport.headers = () => ({});
});

const key = (keyCode: number, k = "") => act(() => void fireEvent.keyDown(window, { keyCode, key: k }));

describe("SMTrading TV app", () => {
  it("first start: the address, then the code; the claim brings the token, kept and sent in the header", async () => {
    render(<TvApp />);
    const input = screen.getByLabelText("Indirizzo del server") as HTMLInputElement;
    expect(input.value).toBe(BASE);
    fireEvent.click(screen.getByRole("button", { name: "Usa questo indirizzo" }));
    expect(await screen.findByLabelText("Codice di abbinamento")).toHaveTextContent("123 456");
    expect(localStorage.getItem(BASE_KEY)).toBe(BASE);
    const post = seen.find((r) => r.method === "POST")!;
    expect(post.headers["x-smt-tv-app"]).toBe("1");
    expect(post.headers["x-smt-tv-token"]).toBeUndefined();

    claim = { state: "claimed", token: "tv-display.abc.sig" };
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" }, { timeout: 3000 })).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_KEY)).toBe("tv-display.abc.sig");
    const read = seen.find((r) => r.url.includes("/selection"))!;
    expect(read.url.startsWith(`${BASE}/api/tv-display/selection`)).toBe(true);
    expect(read.headers["x-smt-tv-token"]).toBe("tv-display.abc.sig");
    expect(read.headers["x-smt-tv-app"]).toBe("1");
    expect(read.headers.cookie).toBeUndefined();
    expect(read.url).not.toContain("token");
  });

  it("the remote: arrows, colours, OK, Back; then a revoked link goes back to the code", async () => {
    localStorage.setItem(BASE_KEY, BASE);
    localStorage.setItem(TOKEN_KEY, "tv-display.abc.sig");
    render(<TvApp />);
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    key(39, "ArrowRight");
    expect(screen.getByRole("heading", { name: "Salute e avvisi" })).toBeInTheDocument();
    key(37, "ArrowLeft");
    key(37, "ArrowLeft");
    expect(screen.getByRole("heading", { name: "Fiume dei flussi" })).toBeInTheDocument();
    key(404);
    expect(screen.getByTestId("screen")).toHaveTextContent("campaign");
    key(403);
    expect(screen.getByTestId("screen")).toHaveTextContent("health");
    key(405);
    expect(screen.getByTestId("screen")).toHaveTextContent("flow");
    key(13, "Enter");
    expect(screen.getByText("Rotazione spenta: premi il tasto blu per avviarla")).toBeInTheDocument();
    key(406);
    expect(screen.getByText("Rotazione attiva")).toBeInTheDocument();
    expect(screen.getByText(/^rotazione 1 di 3/)).toBeInTheDocument();
    key(10252);
    expect(screen.getByText("Rotazione in pausa")).toBeInTheDocument();
    key(10009);
    expect(exit).toHaveBeenCalledOnce();

    selectionStatus = 401;
    expect(await screen.findByText("Collegamento revocato dal portale: abbina di nuovo la TV", undefined, { timeout: 7000 })).toBeInTheDocument();
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(await screen.findByLabelText("Codice di abbinamento")).toBeInTheDocument();
  }, 10_000);

  it("a claim without the token (not the app's entry) says so; blue opens the address", async () => {
    localStorage.setItem(BASE_KEY, BASE);
    claim = { state: "claimed" };
    render(<TvApp />);
    expect(await screen.findByText(/non ha dato il collegamento all'app/, undefined, { timeout: 3000 })).toBeInTheDocument();
    key(406);
    await waitFor(() => expect(screen.getByLabelText("Indirizzo del server")).toBeInTheDocument());
  });
});
