import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FlowEvent, FlowMap, FlowSummary } from "@/api/types";
import FlowRiver from "./FlowRiver";

const hooks = vi.hoisted(() => ({
  useFlowMap: vi.fn(),
  useFlowEvents: vi.fn(),
  useFlowSummary: vi.fn(),
}));
vi.mock("@/api/hooks/flow", () => hooks);

// the renderer needs a real canvas: a stand-in that replays every pushed event at once
vi.mock("@/components/flow/RiverCanvas", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/flow/RiverCanvas")>()),
  RiverCanvas: class {
    launch: ((e: FlowEvent) => void) | null = null;
    follow: ((e: FlowEvent | null) => void) | null = null;
    pushed: FlowEvent[] = [];
    setMap() {}
    setSummary() {}
    setWindow() {}
    setPaused() {}
    setSpeed() {}
    setFollow(on: boolean) {
      const order = this.pushed.find((e) => e.hops[0] === "place_order");
      if (on && order) this.follow?.(order);
    }
    onHover() {}
    onFollow(cb: (e: FlowEvent | null) => void) {
      this.follow = cb;
    }
    onLaunch(cb: (e: FlowEvent) => void) {
      this.launch = cb;
    }
    push(events: FlowEvent[]) {
      this.pushed.push(...events);
      events.forEach((e) => this.launch?.(e));
    }
    destroy() {}
  },
}));

const MAP: FlowMap = {
  nodes: [
    { key: "agent:a1", label: "Portfolio Manager", kind: "agent", column: 0, description: "Decide" },
    { key: "pc", label: "Paperclip", kind: "agent", column: 1, description: "Orchestratore" },
    { key: "mcp", label: "MCP SMTrading", kind: "core", column: 2, description: "La porta" },
    { key: "ng", label: "Neurograph", kind: "ng", column: 3, description: "Forchetta" },
    { key: "eng", label: "Motore ordini", kind: "core", column: 3, description: "Controlli" },
  ],
  links: [
    ["agent:a1", "pc"],
    ["pc", "mcp"],
    ["mcp", "ng"],
  ],
};

const BUSY: FlowSummary = {
  links: { "pc|mcp": { calls: 25, errors: 1, top_tools: [{ tool: "get_forecast", calls: 25 }] } },
  nodes: { pc: 25, mcp: 25 },
  head: { calls_per_min: 25, orders_today: 3, errors_window: 1, mcp_latency_ms: 240 },
};
const QUIET: FlowSummary = {
  links: {},
  nodes: {},
  head: { calls_per_min: 0, orders_today: 0, errors_window: 0, mcp_latency_ms: null },
};

const EVENTS: FlowEvent[] = [
  {
    id: "mcp:7",
    ts: "2026-11-28T10:00:00Z",
    kind: "mcp",
    who: "agent:a1",
    path: ["agent:a1", "pc", "mcp", "ng", "mcp", "pc"],
    hops: ["get_forecast", "get_forecast", "get_forecast", "risposta", "risposta"],
    outcome: "ok",
    latency_ms: 900,
    symbol: "SPY",
  },
];
const NONE: FlowEvent[] = [];

function mockHooks({ summary = BUSY, events = EVENTS, stale = false } = {}) {
  hooks.useFlowMap.mockReturnValue({ data: MAP });
  hooks.useFlowSummary.mockReturnValue({ data: summary });
  hooks.useFlowEvents.mockReturnValue({ events, now: "2026-11-28T10:00:01Z", stale });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FlowRiver", () => {
  it("shows the title and the counters of the summary", () => {
    mockHooks();
    render(<FlowRiver />);
    expect(screen.getByRole("heading", { name: "Fiume dei flussi" })).toBeInTheDocument();
    const counters = screen.getByRole("group", { name: "Contatori" });
    expect(within(counters).getByText("25")).toBeInTheDocument();
    expect(within(counters).getByText("3")).toBeInTheDocument();
    expect(within(counters).getByText("1")).toBeInTheDocument();
    expect(within(counters).getByText("240 ms")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /25 chiamate al minuto/ })).toBeInTheDocument();
    expect(screen.queryByText(/Nessuna chiamata/)).not.toBeInTheDocument();
  });

  it("writes the replayed calls in the live diary", () => {
    mockHooks();
    render(<FlowRiver />);
    const diary = screen.getByRole("log", { name: "Chiamate in diretta" });
    expect(within(diary).getByText("Portfolio Manager")).toBeInTheDocument();
    expect(within(diary).getByText("get_forecast")).toBeInTheDocument();
    expect(diary).toHaveTextContent(/\d\d:\d\d:\d\d Portfolio Manager get_forecast · 900 ms/);
  });

  it("says when there was no call in the window", () => {
    mockHooks({ summary: QUIET, events: NONE });
    render(<FlowRiver />);
    expect(screen.getByRole("status")).toHaveTextContent("Nessuna chiamata negli ultimi 1 minuti");
    expect(screen.getByText("—")).toBeInTheDocument(); // no mean time
  });

  it("warns when the events can not be read, keeping the river", () => {
    mockHooks({ stale: true });
    render(<FlowRiver />);
    expect(screen.getByRole("status")).toHaveTextContent("dati non aggiornati, nuovo tentativo…");
    expect(screen.getByRole("img", { name: /25 chiamate al minuto/ })).toBeInTheDocument();
  });

  it("asks the summary of 5 minutes when the width is set on 5 min", async () => {
    mockHooks();
    render(<FlowRiver />);
    expect(hooks.useFlowSummary).toHaveBeenLastCalledWith(1);
    await userEvent.click(screen.getByRole("button", { name: "5 min" }));
    expect(hooks.useFlowSummary).toHaveBeenLastCalledWith(5);
    expect(screen.getByRole("button", { name: "5 min" })).toHaveAttribute("aria-pressed", "true");
  });

  it("explains the followed order with the names of the map", async () => {
    const order: FlowEvent = {
      ...EVENTS[0],
      id: "mcp:8",
      path: ["agent:a1", "pc", "mcp", "eng"],
      hops: ["place_order", "place_order", "place_order"],
    };
    mockHooks({ events: [order] });
    render(<FlowRiver />);
    await userEvent.click(screen.getByRole("button", { name: "◎ Segui un ordine" }));
    const story = screen.getByText(/chiede a SMTrading/).closest("div")!;
    expect(story).toHaveTextContent("Portfolio Manager");
    expect(story).toHaveTextContent("Paperclip");
    expect(story).toHaveTextContent("MCP SMTrading");
    expect(story).toHaveTextContent("Motore ordini");
    expect(story).toHaveTextContent("SPY");
  });
});
