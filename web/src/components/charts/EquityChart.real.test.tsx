/**
 * The «Andamento» card on the real Lightweight Charts (not the stand-in of src/test): jsdom
 * has no canvas, so a 2D context that draws nothing. The real responses, every hourly range
 * in % and €, each loaded on its click (the chart unmounts while loading: the «6h» crash).
 */
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, expect, it, vi } from "vitest";
import { EquityHistoryCard } from "@/components/EquityHistoryCard";
import { createQueryClient } from "@/queryClient";
import { REAL_HISTORY } from "@/test/fixtures/equityHistory";

vi.unmock("lightweight-charts");

function noopContext(): CanvasRenderingContext2D {
  const known: Record<string, unknown> = {
    measureText: (t: string) => ({ width: t.length * 6, actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 2 }),
    getImageData: () => ({ data: new Uint8ClampedArray(4) }),
    createLinearGradient: () => ({ addColorStop: () => {} }),
    getLineDash: () => [],
  };
  return new Proxy(known, {
    get: (t, k) => (typeof k === "string" && k in t ? t[k] : () => {}),
    set: (t, k, v) => {
      if (typeof k === "string") t[k] = v;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}

const server = setupServer(
  http.get("*/api/portfolios/equity-history", async ({ request }) => {
    const range = new URL(request.url).searchParams.get("range") as keyof typeof REAL_HISTORY;
    await new Promise((r) => setTimeout(r, 5));
    return HttpResponse.json(REAL_HISTORY[range]);
  }),
);
const restore: (() => void)[] = [];
beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = noopContext as unknown as typeof getContext;
  const matchMedia = window.matchMedia;
  window.matchMedia = ((query: string) => ({ matches: false, media: query, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} })) as unknown as typeof matchMedia;
  restore.push(() => (HTMLCanvasElement.prototype.getContext = getContext), () => (window.matchMedia = matchMedia));
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  server.close();
  restore.forEach((r) => r());
});

it("switches 1g → 2h → 6h → 12h and the units on the real library without an error", async () => {
  const errors: unknown[] = [];
  const onError = (e: ErrorEvent) => errors.push(e.error);
  window.addEventListener("error", onError);
  try {
    render(
      <QueryClientProvider client={createQueryClient()}>
        <EquityHistoryCard />
      </QueryClientProvider>,
    );
    await screen.findByTestId("equity-chart");
    for (const [label, unit] of [["2h", "%"], ["6h", "%"], ["12h", "%"], ["6h", "€"], ["2h", "€"], ["1g", "€"], ["6h", "%"]]) {
      await userEvent.click(screen.getByRole("button", { name: unit }));
      await userEvent.click(screen.getByRole("button", { name: label }));
      await screen.findByRole("button", { name: label, pressed: true });
      await screen.findByTestId("equity-chart");
      expect(screen.queryByText("Grafico non disponibile")).toBeNull();
    }
    expect(errors).toEqual([]);
  } finally {
    window.removeEventListener("error", onError);
  }
});
