import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FlowEvent, FlowMap } from "@/api/types";
import { HEALTH_COLORS, MAX_PULSES, QUEUE_MAX, REPLAY_DELAY_MS, RiverCanvas } from "./RiverCanvas";

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

  describe("on the live clock", () => {
    const T0 = Date.parse(NOW);
    let t = 0; // ms since NOW
    const at = (ms: number) => new Date(T0 + ms).toISOString();
    const frames = (ms: number, step = 50) => {
      for (const end = t + ms; t < end; ) {
        t += step;
        vi.setSystemTime(new Date(T0 + t));
        river.drawFrame(1000 + t);
      }
    };
    beforeEach(() => {
      t = 0;
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date(T0));
      river.setMap(MAP);
      river.drawFrame(1000);
    });
    afterEach(() => vi.useRealTimers());

    const burst = (n: number, base: number, prefix: string) =>
      Array.from({ length: n }, (_, i) => event({ id: `${prefix}:${i}`, ts: at(base - (i % 2000)) }));

    it("catches up with the live clock after a 90 s pause, without a comet storm", () => {
      river.push([event({ id: "a" })], at(0));
      river.setPaused(true);
      for (let k = 1; k <= 45; k++) {
        frames(2000, 500);
        river.push(burst(20, t, `p${k}`), at(t));
      }
      expect(river.scheduledCount()).toBe(1); // only the one pushed before the pause
      river.setPaused(false);
      river.push(burst(400, t, "r"), at(t));
      frames(100);
      expect(river.replayLagMs()).toBeGreaterThanOrEqual(REPLAY_DELAY_MS - 100);
      expect(river.replayLagMs()).toBeLessThanOrEqual(REPLAY_DELAY_MS + 200);
      frames(3000);
      expect(river.pulseCount()).toBeLessThanOrEqual(MAX_PULSES + 1);
      river.push([event({ id: "fresh", ts: at(t) })], at(t));
      frames(2400);
      expect(river.scheduledCount()).toBe(1); // due 2.5 s after its time, not before
      frames(300);
      expect(river.scheduledCount()).toBe(0);
    });

    it("catches up after 90 s without frames (a hidden tab)", () => {
      river.push([event({ id: "a" })], at(0));
      t += 90_000;
      vi.setSystemTime(new Date(T0 + t));
      frames(50);
      expect(river.replayLagMs()).toBeLessThanOrEqual(REPLAY_DELAY_MS + 100);
      expect(river.scheduledCount()).toBe(0);
    });

    it("while paused, puts events in the history only and trims it on the live clock", () => {
      const launched: string[] = [];
      river.onLaunch((e) => launched.push(e.id));
      river.setPaused(true);
      river.push(burst(30, t, "h"), at(t));
      expect(river.scheduledCount()).toBe(0);
      expect(launched).toHaveLength(30);
      expect(river.historySize()).toBe(30);
      frames(16 * 60_000, 5_000);
      expect(river.historySize()).toBe(0);
    });

    it("caps the comet queue", () => {
      river.push(
        Array.from({ length: QUEUE_MAX + 300 }, (_, i) => event({ id: `q:${i}`, ts: at(1000 + i) })),
        at(0),
      );
      expect(river.scheduledCount()).toBe(QUEUE_MAX);
    });

    it("draws no ambient particle on a link without traffic", () => {
      river.setSummary({
        links: { "pc|mcp": { calls: 10, errors: 0, top_tools: [] } },
        nodes: {},
        head: { calls_per_min: 10, orders_today: 0, errors_window: 0, mcp_latency_ms: null },
      });
      frames(100);
      expect(river.particleCount("pc|mcp")).toBeGreaterThan(0);
      expect(river.particleCount("mcp|ng")).toBe(0);
    });

    it("lets go of the followed order when a new map drops its comet", () => {
      const followed: (string | null)[] = [];
      river.onFollow((e) => followed.push(e?.id ?? null));
      river.setFollow(true);
      const order = event({ id: "o", path: ["agent:a1", "pc", "mcp", "eng"], hops: ["place_order", "place_order", "place_order"] });
      river.push([order], at(0));
      frames(2600);
      expect(followed).toEqual(["o"]);
      river.setMap({ nodes: MAP.nodes, links: [] });
      expect(followed).toEqual(["o", null]);
    });
  });

  it("caps the events kept before the map arrives", () => {
    river.push(Array.from({ length: 5000 }, (_, i) => event({ id: `w:${i}` })), NOW);
    expect(river.waitingCount()).toBeLessThanOrEqual(2000);
  });

  it("shows events with an unknown node at once and retries them on the next map only", () => {
    const launched: string[] = [];
    river.onLaunch((e) => launched.push(e.id));
    river.setMap(MAP);
    const newcomer = event({ id: "mcp:new", path: ["agent:zz", "pc", "mcp", "ng"], hops: ["get_forecast", "get_forecast", "get_forecast"] });
    river.push([newcomer], NOW);
    expect(launched).toEqual(["mcp:new"]); // in the diary meanwhile
    expect(river.scheduledCount()).toBe(0);
    const withZz: FlowMap = {
      nodes: [...MAP.nodes, { key: "agent:zz", label: "Nuovo", kind: "agent", column: 0, description: "Agente" }],
      links: [...MAP.links, ["agent:zz", "pc"]],
    };
    river.setMap(withZz);
    expect(river.scheduledCount()).toBe(1);

    const other = event({ id: "mcp:other", path: ["agent:yy", "pc", "mcp", "ng"] });
    river.push([other], NOW);
    river.setMap(withZz); // still unknown: dropped
    river.setMap({
      nodes: [...withZz.nodes, { key: "agent:yy", label: "Altro", kind: "agent", column: 0, description: "Agente" }],
      links: [...withZz.links, ["agent:yy", "pc"]],
    });
    expect(river.scheduledCount()).toBe(1);
  });
});

describe("RiverCanvas v2", () => {
  const T0 = Date.parse(NOW);
  const iso = (ms: number) => new Date(T0 + ms).toISOString();
  let frame = 1000;
  const frames = (n: number, step = 50) => {
    for (let i = 0; i < n; i++) river.drawFrame((frame += step));
  };

  /** a context recording what is drawn: fillText strings and stroke colours */
  function recordingRiver() {
    const texts: string[] = [];
    const strokes: string[] = [];
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => {
      const ctx = fakeContext() as unknown as Record<string, unknown>;
      return new Proxy(ctx, {
        get: (t, p: string) => {
          if (p === "fillText") return (s: string) => texts.push(s);
          if (p === "stroke") return () => strokes.push(String(t.strokeStyle));
          return t[p];
        },
        set: (t, p: string, v) => {
          t[p] = v;
          return true;
        },
      }) as never;
    });
    const host = document.createElement("div");
    Object.defineProperty(host, "clientWidth", { value: 1200 });
    Object.defineProperty(host, "clientHeight", { value: 560 });
    const c = document.createElement("canvas");
    host.appendChild(c);
    river.destroy();
    river = new RiverCanvas(c, document.createElement("canvas"));
    return { texts, strokes };
  }

  beforeEach(() => {
    frame = 1000;
    river.setMap(MAP);
  });

  describe("replay", () => {
    it("plays a past window on its own clock at the chosen speed", () => {
      const launched: string[] = [];
      river.onLaunch((e) => launched.push(e.id));
      river.startReplay(T0, T0 + 3_600_000);
      river.addReplay([event({ id: "r:1", ts: iso(30_000) }), event({ id: "r:2", ts: iso(600_000) })]);
      river.setReplayHorizon(T0 + 3_600_000);
      river.setReplaySpeed(60);
      expect(river.replayClock()).toBe(T0);
      frames(1); // first frame: no time yet
      frames(10); // 0.5 s at 60× = 30 s
      expect(launched).toEqual(["r:1"]);
      expect(river.replayClock()).toBe(T0 + 30_000);
      river.setPaused(true);
      frames(100);
      expect(river.replayClock()).toBe(T0 + 30_000);
    });

    it("skips what lies before the time jumped to, and stops at the end of the window", () => {
      const launched: string[] = [];
      river.onLaunch((e) => launched.push(e.id));
      river.startReplay(T0, T0 + 60_000);
      river.addReplay([event({ id: "r:1", ts: iso(10_000) }), event({ id: "r:2", ts: iso(50_000) })]);
      river.setReplayHorizon(T0 + 60_000);
      river.setReplaySpeed(300);
      river.seek(T0 + 40_000);
      frames(20);
      expect(launched).toEqual(["r:2"]);
      expect(river.replayClock()).toBe(T0 + 60_000);
      expect(river.replayEnded()).toBe(true);
    });

    it("merges the pages as they arrive, once each, and caps the comets", () => {
      river.startReplay(T0, T0 + 60_000);
      const page = Array.from({ length: 1500 }, (_, i) => event({ id: `b:${i}`, ts: iso(1000 + (i % 10)) }));
      river.addReplay(page.slice(0, 1000));
      river.addReplay(page.slice(900)); // overlapping page
      expect(river.replayCount()).toBe(1500);
      river.setReplayHorizon(T0 + 60_000);
      river.setReplaySpeed(10);
      frames(10);
      expect(river.pulseCount()).toBeLessThanOrEqual(MAX_PULSES + 1);
    });

    it("waits at the edge of the pages loaded so far, never skipping a page still to come", () => {
      const launched: string[] = [];
      river.onLaunch((e) => launched.push(e.id));
      river.startReplay(T0, T0 + 3_600_000);
      river.setReplaySpeed(300);
      frames(20); // nothing loaded yet: the clock waits at the start
      expect(river.replayClock()).toBe(T0);
      river.addReplay([event({ id: "p1", ts: iso(60_000) })]);
      river.setReplayHorizon(T0 + 60_000);
      frames(20);
      expect(river.replayClock()).toBe(T0 + 60_000);
      river.addReplay([event({ id: "p2", ts: iso(61_000) })]); // the next page
      river.setReplayHorizon(T0 + 3_600_000); // all loaded
      frames(5);
      expect(launched).toEqual(["p1", "p2"]);
    });

    it("ignores live pushes while replaying and goes back to the live clock after", () => {
      river.startReplay(T0 - 3_600_000, T0);
      river.push([event({ id: "live" })], NOW);
      expect(river.scheduledCount()).toBe(0);
      river.stopReplay();
      expect(river.replayClock()).toBeNull();
      river.push([event({ id: "live2" })], NOW);
      expect(river.scheduledCount()).toBe(1);
    });
  });

  it("reports the comet clicked, and a node click the page may take", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(T0));
    try {
      const selected: string[] = [];
      const clicked: string[] = [];
      river.onSelect((e) => selected.push(e.id));
      river.onNodeClick((k) => {
        clicked.push(k);
        return k === "mcp";
      });
      river.startReplay(T0, T0 + 60_000);
      river.addReplay([event({ id: "c:1", ts: iso(100) })]);
      river.setReplayHorizon(T0 + 60_000);
      river.setReplaySpeed(1);
      frames(8);
      const [head] = river.cometHeads();
      expect(head.id).toBe("c:1");
      river.clickAt(head.x, head.y);
      expect(selected).toEqual(["c:1"]);
      const mcp = river.nodePosition("mcp")!;
      river.clickAt(mcp.x, mcp.y);
      expect(clicked).toEqual(["mcp"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("rings each node in the colour of its health and writes the agents' badges", () => {
    const { texts, strokes } = recordingRiver();
    river.setMap(MAP);
    river.setHealth({
      mcp: { state: "ok", reason: "tutto bene" },
      ng: { state: "errore", reason: "get_forecast fallita" },
      eng: { state: "attenzione", reason: "coda lenta" },
    });
    river.setBadges({ "agent:a1": "oggi 1,20 $ · mese 12,00 $ / 50,00 $" });
    river.drawFrame(1000);
    expect(strokes).toContain(HEALTH_COLORS.errore);
    expect(strokes).toContain(HEALTH_COLORS.attenzione);
    expect(strokes).toContain(HEALTH_COLORS.ok);
    expect(texts).toContain("oggi 1,20 $ · mese 12,00 $ / 50,00 $");
  });

  it("takes the width window in minutes («oggi»: since midnight)", () => {
    river.setSummary({
      links: { "pc|mcp": { calls: 600, errors: 0, top_tools: [] } },
      nodes: {},
      head: { calls_per_min: 1, orders_today: 0, errors_window: 0, mcp_latency_ms: null },
    });
    river.setWindow(600);
    expect(river.linkRate("pc|mcp")).toBe(1);
  });
});
