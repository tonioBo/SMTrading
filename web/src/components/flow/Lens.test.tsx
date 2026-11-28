import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FlowEvent, FlowSources, FlowTools } from "@/api/types";
import { Lens } from "./Lens";

const TOOLS: FlowTools = {
  window: 5,
  families: [
    {
      key: "orders",
      label: "ordini",
      tools: [
        { tool: "place_order", dest: "eng", calls: 3, errors: 1, avg_ms: 120 },
        { tool: "list_orders", dest: "db", calls: 0, errors: 0, avg_ms: null },
      ],
    },
    { key: "risk", label: "rischio e previsioni", tools: [{ tool: "get_forecast", dest: "ng", calls: 7, errors: 0, avg_ms: 900 }] },
    { key: "messages", label: "messaggi", tools: [{ tool: "send_telegram", dest: "tg", calls: 0, errors: 0, avg_ms: null }] },
  ],
  other: [],
  links: {},
  truncated: false,
};

const SOURCES: FlowSources = {
  checked_at: "2026-11-28T10:00:00Z",
  stream_status: true,
  polling: ["BTC/EUR"],
  sources: [
    {
      key: "alpaca",
      label: "Alpaca",
      mode: "stream",
      state: "live",
      state_it: "in diretta",
      symbols: 12,
      symbol_list: ["AAPL", "MSFT"],
      updates_per_min: null,
      reads_minute: 4,
      reads_hour: 120,
      note: null,
      health: "ok",
      last_error: null,
      streams: [{ asset_class: "stocks", state: "live", symbols: 12, note: null }],
    },
    {
      key: "yahoo",
      label: "Yahoo",
      mode: "request",
      state: "on_demand",
      state_it: "a richiesta",
      symbols: 0,
      symbol_list: [],
      updates_per_min: null,
      reads_minute: null,
      reads_hour: null,
      note: null,
      health: "error",
      last_error: "HTTP 429",
      streams: [],
    },
  ],
  disabled: ["Finnhub"],
};

function ev(over: Partial<FlowEvent> = {}): FlowEvent {
  return {
    id: "mcp:1",
    ts: "2026-11-28T10:00:00Z",
    kind: "mcp",
    who: "agent:a1",
    path: ["agent:a1", "pc", "mcp", "eng"],
    hops: ["place_order", "place_order", "place_order"],
    outcome: "ok",
    latency_ms: 100,
    symbol: "SPY",
    ...over,
  };
}

function subscriber() {
  const listeners = new Set<(e: FlowEvent) => void>();
  return {
    subscribe: (cb: (e: FlowEvent) => void) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    emit: (e: FlowEvent) => act(() => listeners.forEach((l) => l(e))),
    size: () => listeners.size,
  };
}

describe("Lens", () => {
  it("opens MCP SMTrading into its tool families, each with its tools", () => {
    const sub = subscriber();
    render(<Lens kind="mcp" title="MCP SMTrading" win={5} tools={TOOLS} subscribe={sub.subscribe} onClose={() => {}} />);
    const lens = screen.getByRole("dialog", { name: "Lente su MCP SMTrading" });
    const orders = within(lens).getByRole("listitem", { name: "ordini" });
    expect(orders).toHaveTextContent("3 chiamate");
    expect(orders).toHaveTextContent("1 errore");
    expect(orders).toHaveTextContent("place_order");
    expect(orders).toHaveTextContent("120 ms");
    expect(orders).toHaveTextContent("list_orders");
    expect(within(lens).getByRole("listitem", { name: "rischio e previsioni" })).toHaveTextContent("get_forecast");
    expect(within(lens).getByRole("listitem", { name: "messaggi" })).toHaveTextContent("0 chiamate");
    expect(lens).toHaveTextContent("ultimi 5 min");
  });

  it("runs real comets in the family of each call", () => {
    const sub = subscriber();
    const { container, unmount } = render(<Lens kind="mcp" title="MCP SMTrading" win={5} tools={TOOLS} subscribe={sub.subscribe} onClose={() => {}} />);
    sub.emit(ev());
    sub.emit(ev({ id: "mcp:2", hops: ["get_forecast"] }));
    sub.emit(ev({ id: "fill:1", kind: "fill", path: ["eng", "sim"] })); // not an MCP call
    expect(container.querySelectorAll('[data-comet="orders"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-comet="risk"]')).toHaveLength(1);
    expect(container.querySelectorAll("[data-comet]")).toHaveLength(2);
    unmount();
    expect(sub.size()).toBe(0);
  });

  it("opens Prezzi into the price sources: state, symbols followed, reads, polled symbols, no made-up rate", () => {
    const sub = subscriber();
    const { container } = render(<Lens kind="px" title="Prezzi" win={5} sources={SOURCES} subscribe={sub.subscribe} onClose={() => {}} />);
    const lens = screen.getByRole("dialog", { name: "Lente su Prezzi" });
    const alpaca = within(lens).getByRole("listitem", { name: "Alpaca" });
    expect(alpaca).toHaveTextContent("in diretta");
    expect(alpaca).toHaveTextContent("segue 12 titoli");
    expect(alpaca).toHaveTextContent("4 letture/min");
    expect(alpaca).not.toHaveTextContent("aggiornamenti");
    const yahoo = within(lens).getByRole("listitem", { name: "Yahoo" });
    expect(yahoo).toHaveTextContent("a richiesta");
    expect(yahoo).toHaveTextContent("HTTP 429");
    expect(lens).toHaveTextContent("a lettura periodica: BTC/EUR");
    expect(lens).toHaveTextContent("spente: Finnhub");
    sub.emit(ev({ id: "mcp:3", path: ["agent:a1", "pc", "mcp", "px", "mcp", "pc"], hops: ["get_quote"] }));
    expect(container.querySelectorAll('[data-comet="px"]')).toHaveLength(1);
  });

  it("closes with a click outside or Escape, not with a click inside", async () => {
    const onClose = vi.fn();
    const sub = subscriber();
    render(<Lens kind="mcp" title="MCP SMTrading" win={5} tools={TOOLS} subscribe={sub.subscribe} onClose={onClose} />);
    await userEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("lens-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("waits for the data", () => {
    const sub = subscriber();
    render(<Lens kind="px" title="Prezzi" win="today" subscribe={sub.subscribe} onClose={() => {}} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("caricamento…");
  });

  describe("comets across a refetch", () => {
    afterEach(() => vi.useRealTimers());
    it("keeps launching comets after the tools are read again, and frees the cap", () => {
      vi.useFakeTimers();
      const sub = subscriber();
      const props = { kind: "mcp" as const, title: "MCP SMTrading", win: 5 as const, subscribe: sub.subscribe, onClose: () => {} };
      const { container, rerender } = render(<Lens {...props} tools={TOOLS} />);
      for (let i = 0; i < 8; i++) sub.emit(ev({ id: `mcp:c${i}` }));
      expect(container.querySelectorAll('[data-comet="orders"]')).toHaveLength(8);
      rerender(<Lens {...props} tools={{ ...TOOLS, families: TOOLS.families.map((f) => ({ ...f })) }} />); // a refetch
      act(() => vi.advanceTimersByTime(2_000));
      expect(container.querySelectorAll("[data-comet]")).toHaveLength(0);
      sub.emit(ev({ id: "mcp:after" }));
      expect(container.querySelectorAll('[data-comet="orders"]')).toHaveLength(1);
      expect(sub.size()).toBe(1); // subscribed once
    });
  });

  it("takes the focus and gives it back when it closes", () => {
    const sub = subscriber();
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<Lens kind="mcp" title="MCP SMTrading" win={5} tools={TOOLS} subscribe={sub.subscribe} onClose={() => {}} />);
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("says the counts are live during a replay, and stills the comets for reduced motion", () => {
    const sub = subscriber();
    const { container } = render(<Lens kind="mcp" title="MCP SMTrading" win={5} tools={TOOLS} live subscribe={sub.subscribe} onClose={() => {}} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("conteggi in diretta");
    expect(container.querySelector("style")?.textContent).toContain("prefers-reduced-motion");
  });
});
