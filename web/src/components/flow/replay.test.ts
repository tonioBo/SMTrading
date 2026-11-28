import { describe, expect, it } from "vitest";
import type { FlowEvent } from "@/api/types";
import {
  fromRomeInput,
  presetRange,
  romeMidnight,
  storyTarget,
  SummaryTally,
  summarizeEvents,
  toRomeInput,
  windowLabel,
  windowMinutes,
  zonedToUtc,
} from "./replay";

const at = (iso: string) => Date.parse(iso);

function ev(over: Partial<FlowEvent> = {}): FlowEvent {
  return {
    id: "mcp:1",
    ts: "2026-11-28T10:00:00Z",
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

describe("time zones", () => {
  it("turns a Rome or New York wall time into UTC, summer and winter", () => {
    expect(zonedToUtc(2026, 11, 28, 0, 0, "Europe/Rome")).toBe(at("2026-11-27T23:00:00Z"));
    expect(zonedToUtc(2026, 7, 1, 0, 0, "Europe/Rome")).toBe(at("2026-06-30T22:00:00Z"));
    expect(zonedToUtc(2026, 11, 27, 9, 30, "America/New_York")).toBe(at("2026-11-27T14:30:00Z"));
  });

  it("finds 00:00 in Rome of the day of a time", () => {
    expect(romeMidnight(at("2026-11-28T10:00:00Z"))).toBe(at("2026-11-27T23:00:00Z"));
    expect(romeMidnight(at("2026-11-27T23:30:00Z"))).toBe(at("2026-11-27T23:00:00Z"));
  });

  it("reads and writes the from–to fields in Rome time", () => {
    expect(fromRomeInput("2026-11-28T09:15")).toBe(at("2026-11-28T08:15:00Z"));
    expect(toRomeInput(at("2026-11-28T08:15:00Z"))).toBe("2026-11-28T09:15");
    expect(fromRomeInput("non una data")).toBeNull();
  });
});

describe("width window", () => {
  it("measures «oggi» in minutes since 00:00 in Rome, never under one", () => {
    expect(windowMinutes(5, at("2026-11-28T10:00:00Z"))).toBe(5);
    expect(windowMinutes("today", at("2026-11-28T10:00:00Z"))).toBe(660);
    expect(windowMinutes("today", at("2026-11-27T23:00:10Z"))).toBe(1);
    expect(windowLabel("today")).toBe("oggi");
    expect(windowLabel(15)).toBe("15 min");
  });
});

describe("replay presets", () => {
  const now = at("2026-11-28T10:00:00Z"); // a Saturday, 11:00 in Rome

  it("ultima ora, oggi and ieri", () => {
    expect(presetRange("hour", now)).toEqual({ from: now - 3_600_000, to: now });
    expect(presetRange("today", now)).toEqual({ from: at("2026-11-27T23:00:00Z"), to: now });
    expect(presetRange("yesterday", now)).toEqual({ from: at("2026-11-26T23:00:00Z"), to: at("2026-11-27T23:00:00Z") });
  });

  it("ultima seduta USA: the last weekday's 9:30–16:00 in New York", () => {
    // Saturday → Friday's session
    expect(presetRange("session", now)).toEqual({ from: at("2026-11-27T14:30:00Z"), to: at("2026-11-27T21:00:00Z") });
    // Monday before the open → Friday's session
    expect(presetRange("session", at("2026-11-30T12:00:00Z"))).toEqual({ from: at("2026-11-27T14:30:00Z"), to: at("2026-11-27T21:00:00Z") });
    // during a session: from the open to now
    const live = at("2026-11-30T16:00:00Z");
    expect(presetRange("session", live)).toEqual({ from: at("2026-11-30T14:30:00Z"), to: live });
  });

  it("keeps a window at most 24 hours (the day the clocks change)", () => {
    const r = presetRange("yesterday", at("2026-10-26T10:00:00Z")); // Sunday 25/10 had 25 hours
    expect(r.to - r.from).toBeLessThanOrEqual(24 * 3_600_000);
  });
});

describe("summarizeEvents", () => {
  const links: [string, string][] = [
    ["agent:a1", "pc"],
    ["pc", "mcp"],
    ["mcp", "ng"],
    ["mcp", "eng"],
  ];

  it("counts each link once per event, in the map's direction, per minute", () => {
    const s = summarizeEvents(
      [
        ev(),
        ev({ id: "mcp:2", outcome: "error", latency_ms: 100 }),
        ev({ id: "mcp:3", path: ["agent:a1", "pc", "mcp", "eng"], hops: ["place_order", "place_order", "place_order"], latency_ms: 200 }),
      ],
      10,
      links,
    );
    expect(s.links["pc|mcp"]).toEqual({ calls: 3, errors: 1, top_tools: [{ tool: "get_forecast", calls: 2 }, { tool: "place_order", calls: 1 }] });
    expect(s.links["mcp|ng"].calls).toBe(2);
    expect(s.links["mcp|eng"].calls).toBe(1);
    expect(s.nodes.pc).toBeCloseTo(0.3);
    expect(s.head).toEqual({ calls_per_min: 0.3, orders_today: 1, errors_window: 1, mcp_latency_ms: 400 });
  });

  it("has no traffic and no mean time on an empty window", () => {
    const s = summarizeEvents([], 60, links);
    expect(s.links).toEqual({});
    expect(s.head.mcp_latency_ms).toBeNull();
  });
});

describe("storyTarget", () => {
  it("opens the order of an order event and the MCP call of an agent's order", () => {
    expect(storyTarget(ev({ id: "order:9b1c6c2e-1111-4111-8111-111111111111:sent", kind: "order_sent" }))).toEqual({
      kind: "order",
      id: "9b1c6c2e-1111-4111-8111-111111111111",
    });
    expect(storyTarget(ev({ id: "order:9b1c6c2e-1111-4111-8111-111111111111:rejected", kind: "order_rejected" }))?.kind).toBe("order");
    expect(storyTarget(ev({ id: "mcp:42", hops: ["place_bracket_order"] }))).toEqual({ kind: "mcp", id: "42" });
  });

  it("has no story for the other events", () => {
    expect(storyTarget(ev())).toBeNull(); // get_forecast
    expect(storyTarget(ev({ id: "fill:3", kind: "fill" }))).toBeNull();
    expect(storyTarget(ev({ id: "run:abc", kind: "run" }))).toBeNull();
  });
});

describe("v2 additions from the backend", () => {
  it("prefers the session given by SMTrading", () => {
    const given = { from: at("2026-11-25T14:30:00Z"), to: at("2026-11-25T18:00:00Z") };
    expect(presetRange("session", at("2026-11-28T10:00:00Z"), given)).toEqual(given);
  });

  it("opens the order of a fill that carries it", () => {
    expect(storyTarget(ev({ id: "fill:3", kind: "fill", order_id: "9b1c6c2e-1111-4111-8111-111111111111" }))).toEqual({
      kind: "order",
      id: "9b1c6c2e-1111-4111-8111-111111111111",
    });
  });

  it("adds pages to a tally that gives the same summary as all at once", () => {
    const links: [string, string][] = [["agent:a1", "pc"], ["pc", "mcp"], ["mcp", "ng"]];
    const all = Array.from({ length: 50 }, (_, i) => ev({ id: `mcp:${i}`, outcome: i % 7 ? "ok" : "error", latency_ms: i }));
    const tally = new SummaryTally(links);
    for (let i = 0; i < 50; i += 10) tally.add(all.slice(i, i + 10));
    expect(tally.summary(30)).toEqual(summarizeEvents(all, 30, links));
  });
});
