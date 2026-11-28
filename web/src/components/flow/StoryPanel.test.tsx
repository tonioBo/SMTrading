import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FlowMcpStory, FlowOrderStory } from "@/api/types";
import { StoryPanel } from "./StoryPanel";

const hooks = vi.hoisted(() => ({ useFlowOrderStory: vi.fn(), useFlowMcpStory: vi.fn() }));
vi.mock("@/api/hooks/flow", () => hooks);

const ORDER: FlowOrderStory = {
  order: {
    id: "9b1c6c2e-1111-4111-8111-111111111111",
    symbol: "AAPL",
    side: "buy",
    type: "limit",
    qty: "10",
    notional: null,
    limit_price: "100.5",
    stop_price: null,
    status: "filled",
    status_it: "eseguito",
    role: "entry",
    source: "agent",
    parent_id: null,
    created_at: "2026-11-28T14:30:00Z",
    broker: { kind: "alpaca_paper", label: "Alpaca (prova)", node: "broker:alpaca_paper" },
  },
  agent: { key: "agent:a1", name: "Portfolio Manager" },
  decision: {
    id: "d1",
    kind: "entry",
    summary: "Compro AAPL sul ritracciamento",
    reason: "trend sopra la media a 50 giorni",
    confidence: 70,
    expected: { return_pct: 2.5, horizon_days: 5 },
    at: "2026-11-28T14:29:50Z",
  },
  decision_note: null,
  mcp: { audit_id: 42, tool: "place_bracket_order", outcome: "ok", reason_code: null, latency_ms: 180, symbol: "AAPL", at: "2026-11-28T14:29:59Z" },
  mcp_note: null,
  sent: { at: "2026-11-28T14:30:01Z", broker: "Alpaca (prova)" },
  rejected: null,
  children: [
    { id: "c1", role: "stop", role_it: "stop", type: "stop", side: "sell", status: "accepted", status_it: "in attesa", stop_price: "95", limit_price: null, at: "2026-11-28T14:30:00Z" },
    { id: "c2", role: "target", role_it: "obiettivo", type: "limit", side: "sell", status: "accepted", status_it: "in attesa", stop_price: null, limit_price: "110", at: "2026-11-28T14:30:00Z" },
  ],
  fills: [{ id: "f1", at: "2026-11-28T14:30:05Z", qty: "10", price: "100.4", expected_price: "100.5", expected_from: "limite", slippage_pct: "-0.1", source: "broker" }],
  telegram: [{ category: "order_filled", category_it: "ordine eseguito", status: "sent", at: "2026-11-28T14:30:06Z" }],
  outcome: { status: "filled", status_it: "eseguito", filled_qty: "10", avg_fill_price: "100.4", at: "2026-11-28T14:30:05Z" },
  steps: [
    { at: "2026-11-28T14:29:50Z", step: "decision", label: "decisione: entrata", node: "agent:a1" },
    { at: "2026-11-28T14:29:59Z", step: "mcp", label: "chiamata place_bracket_order", node: "mcp" },
    { at: "2026-11-28T14:30:05Z", step: "fill", label: "eseguito 10 a 100.4", node: "eng" },
  ],
};

const UNLINKED: FlowOrderStory = {
  ...ORDER,
  decision: null,
  decision_note: "decisione non collegata",
  mcp: null,
  mcp_note: "chiamata MCP non trovata",
  children: [],
  fills: [],
  telegram: [],
};

const REFUSED: FlowMcpStory = {
  audit_id: 77,
  at: "2026-11-28T14:31:00Z",
  tool: "place_order",
  agent: { key: "agent:a1", name: "Portfolio Manager" },
  outcome: "error",
  reason_code: "RISK_LIMIT",
  latency_ms: 35,
  symbol: "TSLA",
  path: ["agent:a1", "pc", "mcp", "eng"],
  note: "chiamata rifiutata: nessun ordine creato",
};

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useFlowOrderStory.mockReturnValue({ data: undefined, isLoading: false, isError: false });
  hooks.useFlowMcpStory.mockReturnValue({ data: undefined, isLoading: false, isError: false });
});

describe("StoryPanel", () => {
  it("tells a linked order: decision, MCP call, children, fills against the expected price, Telegram, outcome", () => {
    hooks.useFlowOrderStory.mockReturnValue({ data: ORDER, isLoading: false, isError: false });
    render(<StoryPanel target={{ kind: "order", id: ORDER.order.id }} onClose={() => {}} />);
    const panel = screen.getByRole("dialog", { name: "Storia dell'ordine" });
    expect(hooks.useFlowOrderStory).toHaveBeenCalledWith(ORDER.order.id);
    expect(panel).toHaveTextContent("AAPL");
    expect(panel).toHaveTextContent("Portfolio Manager");
    expect(panel).toHaveTextContent("Compro AAPL sul ritracciamento");
    expect(panel).toHaveTextContent("trend sopra la media a 50 giorni");
    expect(panel).toHaveTextContent("fiducia 70/100");
    expect(panel).toHaveTextContent("return_pct 2,5");
    expect(panel).toHaveTextContent("place_bracket_order");
    expect(panel).toHaveTextContent("180 ms");
    expect(within(panel).getByText(/obiettivo/)).toBeInTheDocument();
    expect(panel).toHaveTextContent("10 a 100,4");
    expect(panel).toHaveTextContent("atteso 100,5 (limite)");
    expect(panel).toHaveTextContent("-0,10%");
    expect(panel).toHaveTextContent("ordine eseguito");
    expect(panel).toHaveTextContent("Esito");
    expect(panel).toHaveTextContent("chiamata place_bracket_order"); // the steps in time order
  });

  it("says when the decision and the MCP call are not linked", () => {
    hooks.useFlowOrderStory.mockReturnValue({ data: UNLINKED, isLoading: false, isError: false });
    render(<StoryPanel target={{ kind: "order", id: ORDER.order.id }} onClose={() => {}} />);
    const panel = screen.getByRole("dialog", { name: "Storia dell'ordine" });
    expect(panel).toHaveTextContent("decisione non collegata");
    expect(panel).toHaveTextContent("chiamata MCP non trovata");
    expect(panel).toHaveTextContent("nessuna esecuzione");
  });

  it("tells a refused call that created no order", () => {
    hooks.useFlowMcpStory.mockReturnValue({ data: REFUSED, isLoading: false, isError: false });
    render(<StoryPanel target={{ kind: "mcp", id: "77" }} onClose={() => {}} />);
    expect(hooks.useFlowMcpStory).toHaveBeenCalledWith("77");
    expect(hooks.useFlowOrderStory).toHaveBeenCalledWith(null);
    const panel = screen.getByRole("dialog", { name: "Storia dell'ordine" });
    expect(panel).toHaveTextContent("place_order");
    expect(panel).toHaveTextContent("TSLA");
    expect(panel).toHaveTextContent("rifiutata");
    expect(panel).toHaveTextContent("RISK_LIMIT");
    expect(panel).toHaveTextContent("chiamata rifiutata: nessun ordine creato");
  });

  it("says when the story can not be read, and closes", async () => {
    hooks.useFlowOrderStory.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    const onClose = vi.fn();
    render(<StoryPanel target={{ kind: "order", id: ORDER.order.id }} onClose={onClose} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("storia non disponibile");
    await userEvent.click(screen.getByRole("button", { name: "Chiudi la storia" }));
    expect(onClose).toHaveBeenCalled();
  });
});
