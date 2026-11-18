import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import Decisions from "@/pages/Decisions";
import { createQueryClient } from "@/queryClient";

const server = setupServer(
  http.get("*/api/agents", () => HttpResponse.json([])),
  http.get("*/api/portfolios", () => HttpResponse.json([])),
  http.get("*/api/decisions", () => HttpResponse.json({ items: [{
    id: 30, created_at: "2026-11-18T21:00:00Z", agent_id: "a1", agent_name: "Performance Auditor",
    portfolio_id: null, portfolio_name: null, kind: "lesson", summary: "Uscita mancata su AMZN",
    rationale: null, symbols: ["AMZN"], details: {}, outcome: null, subject: null, reason: null,
  }], next_cursor: null })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Decisioni page", () => {
  it("lists the agents' decisions, lessons included", async () => {
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter><Decisions /></MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole("heading", { name: "Decisioni" })).toBeInTheDocument();
    expect(await screen.findByText("Uscita mancata su AMZN")).toBeInTheDocument();
    expect(screen.getAllByText("Lezione").length).toBeGreaterThan(0);
  });
});
