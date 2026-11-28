import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FlowEvent, FlowMap } from "@/api/types";
import { RiverCanvas } from "./RiverCanvas";

/** jsdom has no 2D context: a stand-in where every call is a no-op */
function fakeContext(): CanvasRenderingContext2D {
  const gradient = { addColorStop: () => {} };
  const target: Record<string, unknown> = {
    measureText: (s: string) => ({ width: s.length * 7 }),
    createLinearGradient: () => gradient,
    createRadialGradient: () => gradient,
  };
  return new Proxy(target, {
    get: (t, p: string) => (p in t ? t[p] : () => {}),
    set: (t, p: string, v) => {
      t[p] = v;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}

const MAP: FlowMap = {
  nodes: [
    { key: "agent:a1", label: "Portfolio Manager", kind: "agent", column: 0, description: "Decide" },
    { key: "pc", label: "Paperclip", kind: "agent", column: 1, description: "Orchestratore" },
    { key: "mcp", label: "MCP SMTrading", kind: "core", column: 2, description: "La porta" },
    { key: "ng", label: "Neurograph", kind: "ng", column: 3, description: "Forchetta" },
    { key: "eng", label: "Motore ordini", kind: "core", column: 3, description: "Controlli" },
    { key: "sync", label: "BrokerSync", kind: "core", column: 4, description: "Invia" },
    { key: "broker:alpaca_paper", label: "Alpaca (prova)", kind: "broker", column: 5, description: "Broker" },
  ],
  links: [
    ["agent:a1", "pc"],
    ["pc", "mcp"],
    ["mcp", "ng"],
    ["mcp", "eng"],
    ["eng", "sync"],
    ["sync", "broker:alpaca_paper"],
    ["sync", "broker:etoro_demo"], // a link to a node absent from the map: ignored
  ],
};

const NOW = "2026-11-28T10:00:00Z";

function event(over: Partial<FlowEvent> = {}): FlowEvent {
  return {
    id: "mcp:1",
    ts: NOW,
    kind: "mcp",
    who: "agent:a1",
    path: ["agent:a1", "pc", "mcp", "ng", "mcp", "pc"],
    hops: ["get_forecast", "get_forecast", "get_forecast", "risposta", "risposta"],
    outcome: "ok",
    latency_ms: 900,
    symbol: "SPY",
    ...over,
  };
}

let river: RiverCanvas;

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => fakeContext() as never);
  const sized = (w: number, h: number) => {
    const host = document.createElement("div");
    Object.defineProperty(host, "clientWidth", { value: w });
    Object.defineProperty(host, "clientHeight", { value: h });
    const c = document.createElement("canvas");
    host.appendChild(c);
    return c;
  };
  river = new RiverCanvas(sized(1200, 560), sized(700, 100));
});

afterEach(() => {
  river.destroy();
  vi.restoreAllMocks();
});

describe("RiverCanvas", () => {
  it("creates one node per map node and none for keys absent from the map", () => {
    river.setMap(MAP);
    expect(river.nodeKeys().sort()).toEqual(MAP.nodes.map((n) => n.key).sort());
    expect(river.nodeKeys()).not.toContain("broker:etoro_demo");
    expect(river.linkKeys()).toContain("sync|broker:alpaca_paper");
    expect(river.linkKeys()).not.toContain("sync|broker:etoro_demo");
  });

  it("schedules one comet for an event pushed twice", () => {
    river.setMap(MAP);
    river.push([event()], NOW);
    river.push([event()], NOW);
    expect(river.scheduledCount()).toBe(1);
  });

  it("ignores an event whose path has an unknown node, without throwing", () => {
    river.setMap(MAP);
    expect(() => river.push([event({ id: "mcp:2", path: ["agent:zz", "pc", "mcp", "ng"] })], NOW)).not.toThrow();
    expect(river.scheduledCount()).toBe(0);
  });

  it("draws an empty river (no summary, no events) without throwing", () => {
    river.setMap(MAP);
    river.setSummary({ links: {}, nodes: {}, head: { calls_per_min: 0, orders_today: 0, errors_window: 0, mcp_latency_ms: null } });
    expect(() => river.drawFrame(16)).not.toThrow();
  });

  it("replays a pushed event as comets and reports its launch", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(NOW));
    try {
      const launched: string[] = [];
      river.onLaunch((e) => launched.push(e.id));
      river.setMap(MAP);
      river.setSummary({
        links: { "pc|mcp": { calls: 40, errors: 0, top_tools: [{ tool: "get_forecast", calls: 40 }] } },
        nodes: { pc: 40 },
        head: { calls_per_min: 40, orders_today: 0, errors_window: 0, mcp_latency_ms: 900 },
      });
      river.push([event({ outcome: "error" })], NOW);
      expect(launched).toEqual([]);
      for (let f = 0; f < 400; f++) {
        vi.setSystemTime(new Date(Date.parse(NOW) + f * 20));
        river.drawFrame(1000 + f * 20);
      }
      expect(launched).toEqual(["mcp:1"]);
      expect(river.scheduledCount()).toBe(0);
      expect(river.linkDirection("pc|mcp")).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("puts events too old for a comet straight into the history", () => {
    const launched: string[] = [];
    river.onLaunch((e) => launched.push(e.id));
    river.setMap(MAP);
    river.push([event({ id: "mcp:old", ts: "2026-11-28T09:50:00Z" })], NOW);
    expect(launched).toEqual(["mcp:old"]);
    expect(river.scheduledCount()).toBe(0);
  });

  it("keeps the events pushed before the map and plays them once it arrives", () => {
    river.push([event()], NOW);
    expect(river.scheduledCount()).toBe(0);
    river.setMap(MAP);
    expect(river.scheduledCount()).toBe(1);
  });
});
