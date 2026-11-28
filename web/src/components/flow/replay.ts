/**
 * «Fiume dei flussi» v2, pure helpers: the width window («oggi» since 00:00 in Rome), the
 * replay presets (ultima ora, oggi, ieri, ultima seduta USA), the widths of a replayed
 * window computed from its loaded events (the summary route has no from/to), and which
 * events open an order's story.
 */
import type { FlowEvent, FlowSummary, FlowWindow } from "@/api/types";
import { isOrderEvent } from "./RiverCanvas";

export const ROME = "Europe/Rome";
const NEW_YORK = "America/New_York";
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
/** the backend's replay limit */
export const REPLAY_MAX_MS = DAY;
export const REPLAY_SPEEDS = [1, 10, 60, 300] as const;
export type ReplaySpeed = (typeof REPLAY_SPEEDS)[number];

export type PresetKey = "hour" | "today" | "yesterday" | "session";
export const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "hour", label: "ultima ora" },
  { key: "today", label: "oggi" },
  { key: "yesterday", label: "ieri" },
  { key: "session", label: "ultima seduta USA" },
];

export interface Range {
  from: number;
  to: number;
}

const partsFormatters = new Map<string, Intl.DateTimeFormat>();
/** the wall clock of `t` in `tz` */
function wall(t: number, tz: string) {
  let f = partsFormatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      weekday: "short",
      hourCycle: "h23",
    });
    partsFormatters.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return {
    y: Number(p.year),
    mo: Number(p.month),
    d: Number(p.day),
    h: Number(p.hour),
    mi: Number(p.minute),
    s: Number(p.second),
    weekday: p.weekday as string,
  };
}

/** `tz`'s offset from UTC at `t`, ms */
function offset(t: number, tz: string): number {
  const w = wall(t, tz);
  return Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi, w.s) - Math.floor(t / 1000) * 1000;
}

/** a wall time in `tz` (month 1..12) → UTC ms */
export function zonedToUtc(y: number, mo: number, d: number, h: number, mi: number, tz: string): number {
  const guess = Date.UTC(y, mo - 1, d, h, mi);
  let t = guess - offset(guess, tz);
  t = guess - offset(t, tz); // once more, across a clock change
  return t;
}

/** 00:00 in Rome of `t`'s day */
export function romeMidnight(t: number): number {
  const w = wall(t, ROME);
  return zonedToUtc(w.y, w.mo, w.d, 0, 0, ROME);
}

/** a `datetime-local` value read as Rome time; null when it is not a date */
export function fromRomeInput(v: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v);
  if (!m) return null;
  return zonedToUtc(+m[1], +m[2], +m[3], +m[4], +m[5], ROME);
}

export function toRomeInput(t: number): string {
  const w = wall(t, ROME);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${w.y}-${two(w.mo)}-${two(w.d)}T${two(w.h)}:${two(w.mi)}`;
}

/** the width window in minutes (rates per minute): «oggi» since 00:00 in Rome, at least 1 */
export function windowMinutes(w: FlowWindow, now: number): number {
  if (w !== "today") return w;
  return Math.max(1, (now - romeMidnight(now)) / MIN);
}

export function windowLabel(w: FlowWindow): string {
  return w === "today" ? "oggi" : `${w} min`;
}

/** the replay window of a preset; never longer than 24 h; «ultima seduta USA» from
 * SMTrading's calendar when given (`session`), else the last weekday's regular hours */
export function presetRange(key: PresetKey, now: number, session?: Range | null): Range {
  let r: Range;
  if (key === "hour") r = { from: now - HOUR, to: now };
  else if (key === "today") r = { from: romeMidnight(now), to: now };
  else if (key === "yesterday") {
    const today = romeMidnight(now);
    r = { from: romeMidnight(today - 12 * HOUR), to: today };
  } else r = session && session.from < session.to ? session : lastSession(now);
  return { from: r.from, to: Math.min(r.to, r.from + REPLAY_MAX_MS) };
}

/** the regular US session (9:30–16:00 New York) running or last ended on a weekday
 * (holidays are not known here: such a day replays an empty window) */
function lastSession(now: number): Range {
  for (let back = 0; back < 8; back++) {
    const w = wall(now - back * DAY, NEW_YORK);
    if (w.weekday === "Sat" || w.weekday === "Sun") continue;
    const open = zonedToUtc(w.y, w.mo, w.d, 9, 30, NEW_YORK);
    if (open > now) continue;
    return { from: open, to: Math.min(now, zonedToUtc(w.y, w.mo, w.d, 16, 0, NEW_YORK)) };
  }
  return { from: now - HOUR, to: now };
}

/** the tool an event is counted under (as the backend's summary) */
function toolOf(e: FlowEvent): string {
  if (e.kind === "order_sent") return e.hops[1] ?? e.hops[0] ?? e.kind;
  return e.hops[0] ?? e.kind;
}

/** The widths of a replayed window, page by page: `add` each new page once, `summary`
 * gives a FlowSummary over `minutes` — each link once per event, keyed in the map's
 * direction; head: agents' MCP calls per minute, agents' orders accepted, errors, mean
 * MCP time. Adding a page costs that page only. */
export class SummaryTally {
  private readonly forward: Set<string>;
  private readonly links = new Map<string, { calls: number; errors: number; tools: Map<string, number> }>();
  private readonly touches = new Map<string, number>();
  private mcp = 0;
  private mcpMs = 0;
  private errors = 0;
  private orders = 0;

  constructor(mapLinks: [string, string][]) {
    this.forward = new Set(mapLinks.map(([a, b]) => `${a}|${b}`));
  }

  private keyOf(a: string, b: string): string | null {
    if (this.forward.has(`${a}|${b}`)) return `${a}|${b}`;
    return this.forward.has(`${b}|${a}`) ? `${b}|${a}` : null;
  }

  add(events: FlowEvent[]): void {
    for (const e of events) {
      const bad = e.outcome === "error";
      if (bad) this.errors++;
      if (e.kind === "mcp") {
        this.mcp++;
        this.mcpMs += e.latency_ms ?? 0;
        if (!bad && isOrderEvent(e)) this.orders++;
      }
      const keys = new Set<string>();
      for (let i = 0; i < e.path.length - 1; i++) {
        const k = this.keyOf(e.path[i], e.path[i + 1]);
        if (k) keys.add(k);
      }
      const tool = toolOf(e);
      for (const k of keys) {
        let l = this.links.get(k);
        if (!l) this.links.set(k, (l = { calls: 0, errors: 0, tools: new Map() }));
        l.calls++;
        if (bad) l.errors++;
        l.tools.set(tool, (l.tools.get(tool) ?? 0) + 1);
      }
      for (const n of new Set(e.path)) this.touches.set(n, (this.touches.get(n) ?? 0) + 1);
    }
  }

  summary(minutes: number): FlowSummary {
    const per = (n: number) => n / Math.max(1, minutes);
    const links: FlowSummary["links"] = {};
    for (const [k, l] of this.links) {
      links[k] = {
        calls: l.calls,
        errors: l.errors,
        top_tools: [...l.tools.entries()]
          .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
          .slice(0, 3)
          .map(([tool, calls]) => ({ tool, calls })),
      };
    }
    return {
      links,
      nodes: Object.fromEntries([...this.touches].map(([k, n]) => [k, per(n)])),
      head: {
        calls_per_min: per(this.mcp),
        orders_today: this.orders,
        errors_window: this.errors,
        mcp_latency_ms: this.mcp ? Math.round(this.mcpMs / this.mcp) : null,
      },
    };
  }
}

/** a FlowSummary of loaded events over `minutes`, all at once (see SummaryTally) */
export function summarizeEvents(events: FlowEvent[], minutes: number, mapLinks: [string, string][]): FlowSummary {
  const t = new SummaryTally(mapLinks);
  t.add(events);
  return t.summary(minutes);
}

/** the nodes a click opens into a lens */
export type LensKind = "mcp" | "px";
export const LENS_NODES: readonly string[] = ["mcp", "px"];

export type StoryTarget = { kind: "order"; id: string } | { kind: "mcp"; id: string };

/** the story an event opens: its order (order sent or refused, a fill carrying its
 * order), or the MCP call of an agent's place_* (refused calls create no order); null for
 * the others */
export function storyTarget(e: FlowEvent): StoryTarget | null {
  if (e.order_id) return { kind: "order", id: e.order_id };
  const order = /^order:([0-9a-f-]{36}):/i.exec(e.id);
  if (order) return { kind: "order", id: order[1] };
  const mcp = /^mcp:(\d+)$/.exec(e.id);
  if (mcp && isOrderEvent(e)) return { kind: "mcp", id: mcp[1] };
  return null;
}
