/**
 * The «Fiume dei flussi» renderer (Canvas 2D, no React): the drawing of the approved sketch
 * (docs/design-sketches/fiume-pro.html) on real data. Nodes and links come from the map
 * (GET /api/flow/map), band widths from the summary (`links[*].calls` over the window),
 * comets from the real events, each replayed `REPLAY_DELAY_MS` after its time on the server
 * clock, so the traffic flows smoothly between two polls. Nothing is invented: no event,
 * no comet; no traffic, thin bands.
 *
 * v2: a replay mode (a past window on its own clock, at 1×–300×, with jumps), a click on
 * a comet (the page opens its story) or on a node (the page may open its lens), health
 * rings around the nodes and a spend badge under the agents.
 */
import type { FlowEvent, FlowHealthState, FlowMap, FlowSummary } from "@/api/types";

/** colour per node kind (the sketch's palette) */
export const KIND_COLORS: Record<string, string> = {
  agent: "#7c9cff",
  core: "#34e0c4",
  broker: "#ffb347",
  data: "#5fd3ff",
  ng: "#d78bff",
  tg: "#4fc3f7",
  db: "#9fb4d9",
};
export const ERROR_COLOR = "#ff4d6d";
/** the health ring of a node */
export const HEALTH_COLORS: Record<FlowHealthState, string> = {
  ok: "rgba(52,224,196,.35)",
  attenzione: "#ffb347",
  errore: "#ff4d6d",
  sconosciuto: "rgba(139,153,195,.45)",
};
export const kindColor = (kind: string): string => KIND_COLORS[kind] ?? KIND_COLORS.core;

/** the six columns, left to right (FlowNode.column) */
export const COLUMN_HEADS: { title: string; sub: string }[] = [
  { title: "AGENTI", sub: "chi decide" },
  { title: "ORCHESTRA", sub: "sveglia gli agenti" },
  { title: "PORTA MCP", sub: "ingresso in SMTrading" },
  { title: "MOTORE E DATI", sub: "controlli, archivio, prezzi" },
  { title: "USCITE", sub: "invio, simulazione, avvisi" },
  { title: "BROKER", sub: "dove si eseguono gli ordini" },
];

export type Origin = "agent" | "rules" | "system";
/** who starts an event, for the 2-minute timeline */
export const ORIGINS: { key: Origin; label: string; color: string }[] = [
  { key: "agent", label: "Agenti", color: "#8aa4ff" },
  { key: "rules", label: "Regole automatiche", color: "#34e0c4" },
  { key: "system", label: "Sistema", color: "#5fd3ff" },
];
export function originOf(e: FlowEvent): Origin {
  if (e.who.startsWith("agent:")) return "agent";
  return e.who === "rules" ? "rules" : "system";
}

/** an order on its way: an order sent to a broker, or an agent's place_* call */
export function isOrderEvent(e: FlowEvent): boolean {
  return e.kind === "order_sent" || (e.kind === "mcp" && /^place_/.test(e.hops[0] ?? ""));
}

export const REPLAY_DELAY_MS = 2_500;
/** a late event (its time already played) is spread over the next poll interval */
const SPREAD_MS = 2_000;
/** older than this behind the replay clock: history only (diary, timeline), no comet */
const LATE_MS = 15_000;
const KEEP_MS = 15 * 60_000;
const TIMELINE_S = 120;
/** comets drawn at once: beyond, an event joins the diary and the timeline only */
export const MAX_PULSES = 160;
/** events waiting for their comet: beyond, the oldest join the history */
export const QUEUE_MAX = 500;
/** replay: comets started in one frame at most (a dense second at 300×), the rest join
 * the diary and the timeline */
export const REPLAY_BURST = 40;
/** a click this close to a comet's head selects it */
const COMET_HIT = 12;
/** events kept before the map arrives, and with a node the map does not have yet */
const WAITING_MAX = 2_000;
const BW = 14; // node bar width
const TOP = 54; // below the column heads
const BOTTOM_PAD = 18;
const FONT = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif";
const MONO = "'JetBrains Mono', ui-monospace, Consolas, monospace";

export type HoverTarget = { node?: string; link?: string; x: number; y: number };

interface Port {
  y0: number;
  y1: number;
}
interface Particle {
  t: number;
  lat: number;
  v: number;
  f: boolean;
}
interface RNode {
  key: string;
  label: string;
  kind: string;
  column: number;
  col: string;
  x: number;
  y: number;
  h: number;
  heat: number;
  right: RLink[];
  left: RLink[];
}
interface RLink {
  key: string;
  L: string;
  R: string;
  loop: boolean;
  w: number;
  wT: number;
  rate: number;
  glow: number;
  parts: Particle[];
  dirF: number;
  dirB: number;
  pL: Port | null;
  pR: Port | null;
  _pL: Port | null;
  _pR: Port | null;
}
interface Pulse {
  evId: string;
  ev: FlowEvent;
  link: RLink;
  fwd: boolean;
  t: number;
  col: string;
  label: string;
  done: () => void;
}
interface Flash {
  key: string;
  col: string;
  t: number;
  bad: boolean;
}
interface Seen {
  t: number;
  ev: FlowEvent;
}
interface Queued {
  at: number;
  ev: FlowEvent;
}

function hexA(h: string, a: number): string {
  const n = parseInt(h.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function cub(p0: number, p1: number, p2: number, p3: number, t: number): number {
  const u = 1 - t;
  return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
}

export class RiverCanvas {
  private readonly cv: HTMLCanvasElement;
  private readonly tl: HTMLCanvasElement;
  private readonly g: CanvasRenderingContext2D | null;
  private readonly tg: CanvasRenderingContext2D | null;
  private W = 0;
  private H = 0;
  private TW = 0;
  private TH = 0;
  private DPR = 1;
  private nodes = new Map<string, RNode>();
  private links = new Map<string, RLink>();
  private summary: FlowSummary | null = null;
  /** the width window in minutes (rates per minute, prevailing directions) */
  private win = 1;
  /** replay: the window and the next event of `recent` to play (in replay `recent` holds
   * every event loaded, in time order) */
  private replay: { from: number; to: number; speed: number; next: number; horizon: number } | null = null;
  private health: Record<string, { state: FlowHealthState }> = {};
  private badges: Record<string, string> = {};
  private heads: { id: string; x: number; y: number; ev: FlowEvent }[] = [];
  private selectCb: ((e: FlowEvent) => void) | null = null;
  private nodeClickCb: ((key: string) => boolean) | null = null;
  private speed = 1;
  private paused = false;
  private follow = false;
  private followRun: { id: string; set: Set<string> } | null = null;
  private hoverNode: string | null = null;
  private lockNode: string | null = null;
  private focus: Set<string> | null = null;
  private pulses: Pulse[] = [];
  private flashes: Flash[] = [];
  private seen = new Set<string>();
  private waiting: FlowEvent[] = []; // pushed before the map arrived
  private unknown: FlowEvent[] = []; // a node the map does not have yet: retried on the next map
  private queue: Queued[] = [];
  private recent: Seen[] = [];
  private offset = 0; // server clock − local clock, ms
  private playT: number | null = null; // the replay clock, server ms
  private fit = 1; // shrinks the bands when a column would overflow
  private last = 0;
  private lastStats = -1e9;
  private raf = 0;
  private hoverCb: ((h: HoverTarget | null) => void) | null = null;
  private launchCb: ((e: FlowEvent) => void) | null = null;
  private followCb: ((e: FlowEvent | null) => void) | null = null;
  private observer: ResizeObserver | null = null;
  private readonly unlisten: (() => void)[] = [];

  constructor(canvas: HTMLCanvasElement, timeline: HTMLCanvasElement) {
    this.cv = canvas;
    this.tl = timeline;
    this.g = canvas.getContext("2d");
    this.tg = timeline.getContext("2d");
    const resize = () => this.resize();
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(resize);
      [canvas.parentElement, timeline.parentElement].forEach((el) => el && this.observer!.observe(el));
    } else {
      window.addEventListener("resize", resize);
      this.unlisten.push(() => window.removeEventListener("resize", resize));
    }
    this.listen(canvas, "mousemove", (e) => this.onMove(e as MouseEvent));
    this.listen(canvas, "mouseleave", () => {
      this.hoverNode = null;
      canvas.style.cursor = "default";
      this.hoverCb?.(null);
    });
    this.listen(canvas, "click", (e) => this.clickAt((e as MouseEvent).offsetX, (e as MouseEvent).offsetY));
    this.resize();
    if (typeof requestAnimationFrame !== "undefined") {
      const loop = (t: number) => {
        this.drawFrame(t);
        this.raf = requestAnimationFrame(loop);
      };
      this.raf = requestAnimationFrame(loop);
    }
  }

  // ------------------------------------------------------------------ public API

  setMap(m: FlowMap): void {
    const nodes = new Map<string, RNode>();
    for (const n of m.nodes) {
      const old = this.nodes.get(n.key);
      nodes.set(n.key, {
        key: n.key,
        label: n.label,
        kind: n.kind,
        column: n.column,
        col: kindColor(n.kind),
        x: old?.x ?? 0,
        y: old?.y ?? 0,
        h: old?.h ?? 22,
        heat: old?.heat ?? 0,
        right: [],
        left: [],
      });
    }
    const links = new Map<string, RLink>();
    for (const [a, b] of m.links) {
      const A = nodes.get(a);
      const B = nodes.get(b);
      if (!A || !B) continue; // a link to a node the map does not have
      const key = `${a}|${b}`;
      const old = this.links.get(key);
      const [L, R] = A.column <= B.column ? [a, b] : [b, a];
      links.set(key, {
        key,
        L,
        R,
        loop: A.column === B.column,
        w: old?.w ?? 1.5,
        wT: old?.wT ?? 1.5,
        rate: old?.rate ?? 0,
        glow: old?.glow ?? 0,
        parts: old?.parts ?? [],
        dirF: old?.dirF ?? 0,
        dirB: old?.dirB ?? 0,
        pL: null,
        pR: null,
        _pL: null,
        _pR: null,
      });
    }
    this.nodes = nodes;
    this.links = links;
    this.pulses = this.pulses.filter((p) => links.get(p.link.key) === p.link);
    this.flashes = this.flashes.filter((f) => nodes.has(f.key));
    const fr = this.followRun;
    if (fr && !this.pulses.some((p) => p.evId === fr.id)) {
      this.followRun = null; // its comet was on a link the new map dropped
      this.followCb?.(null);
    }
    this.applySummary();
    if (!nodes.size) return;
    const retry = [...this.waiting, ...this.unknown];
    this.waiting = [];
    this.unknown = [];
    if (retry.length) this.accept(retry, false); // still unknown now: dropped
  }

  setSummary(s: FlowSummary): void {
    this.summary = s;
    this.applySummary();
  }

  /** the width window of the summary in minutes (rates per minute, prevailing
   * directions): 1, 5, 15, the minutes since midnight («oggi»), or a replayed window */
  setWindow(minutes: number): void {
    this.win = Math.max(1, minutes);
    this.lastStats = -1e9;
    this.applySummary();
  }

  linkRate(key: string): number {
    return this.links.get(key)?.rate ?? 0;
  }

  /** live events; `serverNow` (the poll's server clock) sets the clock offset — events
   * from another read (Paperclip runs) come without it */
  push(events: FlowEvent[], serverNow?: string): void {
    if (this.replay) return; // the live traffic waits while a past window plays
    const server = serverNow ? Date.parse(serverNow) : NaN;
    if (Number.isFinite(server)) this.offset = server - Date.now();
    if (this.playT === null) this.playT = this.liveNow() - REPLAY_DELAY_MS;
    const fresh = events.filter((e) => !this.seen.has(e.id));
    fresh.forEach((e) => this.seen.add(e.id));
    if (this.seen.size > 10_000) this.seen = new Set([...this.seen].slice(-5_000));
    if (!this.nodes.size) {
      this.waiting.push(...fresh);
      if (this.waiting.length > WAITING_MAX) this.waiting.splice(0, this.waiting.length - WAITING_MAX);
      return;
    }
    this.accept(fresh);
  }

  setPaused(b: boolean): void {
    this.paused = b;
  }

  setSpeed(x: 0.5 | 1 | 2): void {
    this.speed = x;
  }

  // ---- replay

  /** play [from, to] (server ms) instead of the live traffic: everything live is dropped,
   * the clock stands at `from` (paused or not, as set) */
  startReplay(from: number, to: number): void {
    this.clearTraffic();
    this.replay = { from, to, speed: this.replay?.speed ?? 1, next: 0, horizon: from };
    this.recent = [];
    this.playT = from;
  }

  /** a loaded page (or several): merged once each by id, in time order */
  addReplay(events: FlowEvent[]): void {
    const r = this.replay;
    if (!r) return;
    const fresh: Seen[] = [];
    for (const ev of events) {
      const t = Date.parse(ev.ts);
      if (!Number.isFinite(t) || this.seen.has(ev.id)) continue;
      this.seen.add(ev.id);
      fresh.push({ t, ev });
    }
    if (!fresh.length) return;
    fresh.sort((a, b) => a.t - b.t);
    // the pages come in time order: a linear merge keeps a long window cheap
    const old = this.recent;
    const merged: Seen[] = new Array(old.length + fresh.length);
    let i = 0;
    let j = 0;
    let k = 0;
    while (i < old.length && j < fresh.length) merged[k++] = old[i].t <= fresh[j].t ? old[i++] : fresh[j++];
    while (i < old.length) merged[k++] = old[i++];
    while (j < fresh.length) merged[k++] = fresh[j++];
    this.recent = merged;
    r.next = this.upper(this.playT ?? r.from);
    this.lastStats = -1e9;
  }

  /** how far the pages loaded so far reach (the window's end once all are read): the
   * clock waits there, so a page still to come is never skipped */
  setReplayHorizon(t: number): void {
    if (this.replay) this.replay.horizon = Math.min(this.replay.to, t);
  }

  setReplaySpeed(x: number): void {
    if (this.replay) this.replay.speed = x;
  }

  /** jump to `t` in the window: what lies before is history, no comet */
  seek(t: number): void {
    const r = this.replay;
    if (!r) return;
    this.playT = Math.max(r.from, Math.min(r.to, t));
    this.pulses = [];
    this.flashes = [];
    this.followRun = null;
    r.next = this.upper(this.playT);
    this.lastStats = -1e9;
  }

  /** back to the live traffic (the next push restarts the live clock) */
  stopReplay(): void {
    if (!this.replay) return;
    this.replay = null;
    this.clearTraffic();
    this.recent = [];
    this.playT = null;
  }

  /** the time shown in replay (server ms), null live */
  replayClock(): number | null {
    return this.replay ? this.playT : null;
  }

  replayEnded(): boolean {
    return !!this.replay && this.playT !== null && this.playT >= this.replay.to;
  }

  replayCount(): number {
    return this.replay ? this.recent.length : 0;
  }

  // ---- v2 decorations and clicks

  setHealth(h: Record<string, { state: FlowHealthState }>): void {
    this.health = h;
  }

  /** a short text under a node (the agents' spend) */
  setBadges(b: Record<string, string>): void {
    this.badges = b;
  }

  /** a comet clicked: its event */
  onSelect(cb: (e: FlowEvent) => void): void {
    this.selectCb = cb;
  }

  /** a node clicked: true when the page takes it (a lens), else it locks the focus */
  onNodeClick(cb: (key: string) => boolean): void {
    this.nodeClickCb = cb;
  }

  /** the comets' heads drawn in the last frame */
  cometHeads(): { id: string; x: number; y: number }[] {
    return this.heads.map(({ id, x, y }) => ({ id, x, y }));
  }

  nodePosition(key: string): { x: number; y: number } | null {
    const n = this.nodes.get(key);
    return n ? { x: n.x, y: n.y } : null;
  }

  clickAt(x: number, y: number): void {
    let best: (typeof this.heads)[number] | null = null;
    let bd = COMET_HIT;
    for (const h of this.heads) {
      const d = Math.hypot(h.x - x, h.y - y);
      if (d < bd) {
        bd = d;
        best = h;
      }
    }
    if (best) {
      this.selectCb?.(best.ev);
      return;
    }
    const h = this.hitTest(x, y);
    if (h.node && this.nodeClickCb?.(h.node)) return;
    this.lockNode = h.node && h.node !== this.lockNode ? h.node : null;
    this.refreshFocus();
  }

  setFollow(b: boolean): void {
    this.follow = b;
    if (!b && this.followRun) {
      this.followRun = null;
      this.followCb?.(null);
    }
  }

  onHover(cb: (h: HoverTarget | null) => void): void {
    this.hoverCb = cb;
  }

  /** an event starts its comet (or, too old for one, joins the history) */
  onLaunch(cb: (e: FlowEvent) => void): void {
    this.launchCb = cb;
  }

  /** «segui un ordine»: the order being followed, null when it has arrived */
  onFollow(cb: (e: FlowEvent | null) => void): void {
    this.followCb = cb;
  }

  /** the prevailing direction of a link in the window, [from, to], null with no traffic */
  linkDirection(key: string): [string, string] | null {
    const l = this.links.get(key);
    if (!l || l.dirF + l.dirB === 0) return null;
    return l.dirF >= l.dirB ? [l.L, l.R] : [l.R, l.L];
  }

  nodeKeys(): string[] {
    return [...this.nodes.keys()];
  }

  linkKeys(): string[] {
    return [...this.links.keys()];
  }

  /** events waiting for their replay time */
  scheduledCount(): number {
    return this.queue.length;
  }

  waitingCount(): number {
    return this.waiting.length;
  }

  pulseCount(): number {
    return this.pulses.length;
  }

  /** events kept for the timeline, the directions and the focus (last 15 minutes) */
  historySize(): number {
    return this.recent.length;
  }

  particleCount(key: string): number {
    return this.links.get(key)?.parts.length ?? 0;
  }

  /** how far the replay clock is behind the live (server) clock, ms */
  replayLagMs(): number {
    return this.playT === null ? 0 : this.liveNow() - this.playT;
  }

  destroy(): void {
    if (this.raf && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.observer?.disconnect();
    this.unlisten.forEach((f) => f());
    this.hoverCb = this.launchCb = this.followCb = null;
    this.selectCb = null;
    this.nodeClickCb = null;
  }

  // ------------------------------------------------------------------ events

  private listen(el: EventTarget, type: string, f: (e: Event) => void): void {
    el.addEventListener(type, f);
    this.unlisten.push(() => el.removeEventListener(type, f));
  }

  private clearTraffic(): void {
    this.queue = [];
    this.pulses = [];
    this.flashes = [];
    this.heads = [];
    this.seen = new Set();
    this.waiting = [];
    this.unknown = [];
    if (this.followRun) {
      this.followRun = null;
      this.followCb?.(null);
    }
    this.lastStats = -1e9;
  }

  /** the first index of `recent` later than t (binary search) */
  private upper(t: number): number {
    let lo = 0;
    let hi = this.recent.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.recent[mid].t <= t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  private liveNow(): number {
    return Date.now() + this.offset;
  }

  /** the link a hop a → b travels, and whether it goes forward on it */
  private hopLink(a: string, b: string): { link: RLink; fwd: boolean } | null {
    const link = this.links.get(`${a}|${b}`) ?? this.links.get(`${b}|${a}`);
    return link ? { link, fwd: link.L === a } : null;
  }

  private valid(e: FlowEvent): boolean {
    if (!e.path.length || !e.path.every((k) => this.nodes.has(k))) return false;
    for (let i = 0; i < e.path.length - 1; i++) if (!this.hopLink(e.path[i], e.path[i + 1])) return false;
    return true;
  }

  /** new events: a comet each when due, history only when too old or while paused; an
   * event with a node the map does not have is shown at once and, if `keepUnknown`,
   * retried on the next map */
  private accept(events: FlowEvent[], keepUnknown = true): void {
    const play = this.playT ?? this.liveNow() - REPLAY_DELAY_MS;
    const dated = events.filter((e) => Number.isFinite(Date.parse(e.ts)));
    const ok = dated.filter((e) => this.valid(e)).sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    if (keepUnknown) {
      const bad = dated.filter((e) => !this.valid(e));
      bad.forEach((e) => this.launchCb?.(e)); // the diary shows it meanwhile
      this.unknown.push(...bad);
      if (this.unknown.length > WAITING_MAX) this.unknown.splice(0, this.unknown.length - WAITING_MAX);
    }
    const late = ok.filter((e) => {
      const t = Date.parse(e.ts);
      return t < play && t >= play - LATE_MS;
    });
    let li = 0;
    for (const ev of ok) {
      const t = Date.parse(ev.ts);
      this.recent.push({ t, ev });
      if (this.paused) this.launchCb?.(ev); // paused: diary and timeline only
      else if (t >= play) this.queue.push({ at: t, ev });
      else if (t >= play - LATE_MS) this.queue.push({ at: play + (li++ / Math.max(1, late.length)) * SPREAD_MS, ev });
      else this.launchCb?.(ev); // history: in the diary and the timeline, no comet
    }
    this.queue.sort((a, b) => a.at - b.at);
    if (this.queue.length > QUEUE_MAX) {
      this.queue.splice(0, this.queue.length - QUEUE_MAX).forEach((q) => this.launchCb?.(q.ev));
    }
    this.recent.sort((a, b) => a.t - b.t);
    this.lastStats = -1e9;
  }

  private run(ev: FlowEvent): void {
    this.launchCb?.(ev);
    if (this.follow && !this.followRun && isOrderEvent(ev)) {
      this.followRun = { id: ev.id, set: new Set(ev.path) };
      this.followCb?.(ev);
    }
    const followed = this.followRun?.id === ev.id;
    if (this.pulses.length > MAX_PULSES && !followed) return; // too busy to draw one more
    const path = ev.path;
    let i = 0;
    const hop = () => {
      if (i >= path.length - 1) {
        if (this.followRun?.id === ev.id) {
          this.followRun = null;
          this.followCb?.(null);
        }
        return;
      }
      const a = path[i];
      const b = path[i + 1];
      const h = this.hopLink(a, b);
      const A = this.nodes.get(a);
      if (!h || !A) {
        i++;
        hop();
        return;
      }
      const bad = ev.outcome === "error" && i === path.length - 2;
      const col = bad ? ERROR_COLOR : A.col;
      const link = h.link;
      this.pulses.push({
        evId: ev.id,
        ev,
        link,
        fwd: h.fwd,
        t: 0,
        col,
        label: ev.hops[i] ?? "",
        done: () => {
          this.flashes.push({ key: b, col, t: 0, bad });
          const B = this.nodes.get(b);
          if (B) B.heat = 1;
          link.glow = 1;
          i++;
          hop();
        },
      });
    };
    hop();
  }

  // ------------------------------------------------------------------ layout

  private resize(): void {
    this.DPR = Math.min(2, (typeof devicePixelRatio === "number" && devicePixelRatio) || 1);
    const size = (c: HTMLCanvasElement, ctx: CanvasRenderingContext2D | null) => {
      const host = c.parentElement;
      const w = host?.clientWidth ?? 0;
      const h = host?.clientHeight ?? 0;
      c.width = Math.max(1, Math.round(w * this.DPR));
      c.height = Math.max(1, Math.round(h * this.DPR));
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
      ctx?.setTransform(this.DPR, 0, 0, this.DPR, 0, 0);
      return [w, h];
    };
    [this.W, this.H] = size(this.cv, this.g);
    [this.TW, this.TH] = size(this.tl, this.tg);
  }

  private applySummary(): void {
    const s = this.summary;
    this.links.forEach((l) => {
      const calls = s?.links[l.key]?.calls ?? 0;
      l.rate = calls / this.win;
    });
  }

  private labelWidth(n: RNode): number {
    if (!this.g) return n.label.length * 7.8;
    this.g.font = `600 13.5px ${FONT}`;
    return this.g.measureText(n.label).width;
  }

  private columns(): RNode[][] {
    const cols: RNode[][] = COLUMN_HEADS.map(() => []);
    this.nodes.forEach((n) => cols[Math.max(0, Math.min(cols.length - 1, n.column))].push(n));
    return cols;
  }

  private margins(cols: RNode[][]): { left: number; right: number } {
    const widest = (list: RNode[]) => list.reduce((m, n) => Math.max(m, this.labelWidth(n)), 0);
    const lastCol = cols.reduce((m, c, i) => (c.length ? i : m), 0);
    return {
      left: Math.max(60, widest(cols[0]) + 34),
      right: Math.max(60, widest(cols[lastCol]) + 34),
    };
  }

  private colX(i: number, m: { left: number; right: number }): number {
    return m.left + (i * Math.max(0, this.W - m.left - m.right)) / (COLUMN_HEADS.length - 1);
  }

  private bottom(): number {
    return Math.max(TOP + 120, this.H - BOTTOM_PAD);
  }

  private layout(cols: RNode[][], m: { left: number; right: number }): void {
    const avail = this.bottom() - TOP;
    let maxR = 0;
    this.links.forEach((l) => (maxR = Math.max(maxR, l.rate)));
    const k = Math.min(3.2, (avail * 0.34) / Math.max(1, maxR)) * this.fit;
    this.links.forEach((l) => {
      l.wT = 1.5 + l.rate * k;
      l.w += (l.wT - l.w) * 0.08;
    });
    this.nodes.forEach((n) => {
      n.right = [];
      n.left = [];
    });
    this.links.forEach((l) => {
      const L = this.nodes.get(l.L)!;
      const R = this.nodes.get(l.R)!;
      L.right.push(l);
      if (l.loop) R.right.push(l);
      else R.left.push(l);
    });
    const sum = (side: RLink[]) => side.reduce((a, l) => a + l.w, 0);
    this.nodes.forEach((n) => (n.h = Math.max(22, sum(n.right), sum(n.left))));
    let overflow = false;
    cols.forEach((c, i) => {
      if (!c.length) return;
      const x = this.colX(i, m);
      const tot = c.reduce((a, n) => a + n.h, 0);
      if (tot + 14 * (c.length + 1) > avail) overflow = true;
      const gap = Math.max(14, (avail - tot) / (c.length + 1));
      let y = TOP + gap;
      c.forEach((n) => {
        n.x = x;
        n.y = y + n.h / 2;
        y += n.h + gap;
      });
    });
    this.fit = overflow ? Math.max(0.15, this.fit * 0.97) : Math.min(1, this.fit * 1.01);
    // ports, ordered by the other node's height
    this.nodes.forEach((n) => {
      const other = (l: RLink) => this.nodes.get(l.L === n.key ? l.R : l.L)!;
      (["right", "left"] as const).forEach((side) => {
        const list = side === "right" ? n.right : n.left;
        list.sort((a, b) => other(a).y - other(b).y);
        const tot = sum(list);
        let y = n.y - tot / 2;
        list.forEach((l) => {
          const p = { y0: y, y1: y + l.w };
          y += l.w;
          if (side === "left") l.pR = p;
          else if (l.L === n.key) l.pL = p;
          else l.pR = p;
        });
      });
    });
    this.links.forEach((l) => {
      l._pL = l.pL;
      l._pR = l.pR;
      l.pL = null;
      l.pR = null;
    });
  }

  private ends(l: RLink) {
    const A = this.nodes.get(l.L)!;
    const B = this.nodes.get(l.R)!;
    return { xa: A.x + BW / 2, xb: l.loop ? B.x + BW / 2 : B.x - BW / 2, a: l._pL!, b: l._pR! };
  }

  private ctrl(l: RLink, xa: number, xb: number): [number, number] {
    if (l.loop) return [xa + 110, xb + 110];
    const mid = (xa + xb) / 2;
    return [mid, mid];
  }

  private pointOn(l: RLink, t: number, lat: number): [number, number] {
    const { xa, xb, a, b } = this.ends(l);
    const [c1, c2] = this.ctrl(l, xa, xb);
    const ya = a.y0 + (a.y1 - a.y0) * lat;
    const yb = b.y0 + (b.y1 - b.y0) * lat;
    return [cub(xa, c1, c2, xb, t), cub(ya, ya, yb, yb, t)];
  }

  // ------------------------------------------------------------------ hover

  private riverOf(k: string): Set<string> {
    const s = new Set<string>();
    this.links.forEach((l) => {
      if (l.L === k || l.R === k) s.add(l.key);
    });
    // live: the last 15 minutes kept; replay: the 15 minutes before the time shown
    const play = this.playT ?? this.liveNow();
    const start = this.replay ? this.upper(play - KEEP_MS) : 0;
    const end = this.replay ? this.upper(play) : this.recent.length;
    for (let j = start; j < end; j++) {
      const { ev } = this.recent[j];
      if (!ev.path.includes(k)) continue;
      for (let i = 0; i < ev.path.length - 1; i++) {
        const h = this.hopLink(ev.path[i], ev.path[i + 1]);
        if (h) s.add(h.link.key);
      }
    }
    return s;
  }

  private refreshFocus(): void {
    const k = this.lockNode ?? this.hoverNode;
    this.focus = k ? this.riverOf(k) : null;
  }

  private labelHit(n: RNode, mx: number, my: number): boolean {
    const w = this.labelWidth(n) + 10;
    const left = n.column === 0;
    return Math.abs(my - n.y) < 12 && (left ? mx < n.x && mx > n.x - 16 - w : mx > n.x && mx < n.x + 16 + w);
  }

  private hitTest(mx: number, my: number): { node?: string; link?: string } {
    for (const n of this.nodes.values()) {
      if ((Math.abs(mx - n.x) < BW / 2 + 6 && Math.abs(my - n.y) < n.h / 2 + 6) || this.labelHit(n, mx, my)) return { node: n.key };
    }
    let best: RLink | null = null;
    let bd = 1e9;
    this.links.forEach((l) => {
      if (!l._pL || !l._pR) return;
      for (let s = 0; s <= 24; s++) {
        const [x, y] = this.pointOn(l, s / 24, 0.5);
        const d = Math.hypot(x - mx, y - my);
        if (d < Math.max(6, l.w / 2 + 3) && d < bd) {
          bd = d;
          best = l;
        }
      }
    });
    return best ? { link: (best as RLink).key } : {};
  }

  private onMove(e: MouseEvent): void {
    const h = this.hitTest(e.offsetX, e.offsetY);
    const node = h.node ?? null;
    if (node !== this.hoverNode) {
      this.hoverNode = node;
      this.refreshFocus();
    }
    this.cv.style.cursor = h.node || h.link ? "pointer" : "default";
    this.hoverCb?.(h.node || h.link ? { ...h, x: e.offsetX, y: e.offsetY } : null);
  }

  // ------------------------------------------------------------------ frame

  /** one animation frame at time `now` (ms, performance clock) */
  drawFrame(now: number): void {
    const raw = this.last ? Math.min(0.05, Math.max(0, (now - this.last) / 1000)) : 0;
    this.last = now;
    const dt = this.paused ? 0 : raw * (this.replay ? 1 : this.speed);
    this.advanceClock(raw);
    if (now - this.lastStats > 1000) this.stats(now);
    const g = this.g;
    if (!g || this.W <= 0 || this.H <= 0) {
      this.drawTimeline();
      return;
    }
    const cols = this.columns();
    const m = this.margins(cols);
    if (this.nodes.size) this.layout(cols, m);
    const fr = this.followRun;
    const focus = fr ? null : this.focus;
    g.clearRect(0, 0, this.W, this.H);
    this.drawHeads(g, cols, m);
    const bot = this.bottom();
    // column guides
    cols.forEach((c, i) => {
      if (!c.length) return;
      const x = this.colX(i, m);
      const gr = g.createLinearGradient(0, TOP - 20, 0, bot);
      gr.addColorStop(0, "rgba(120,140,220,0)");
      gr.addColorStop(0.5, "rgba(120,140,220,.07)");
      gr.addColorStop(1, "rgba(120,140,220,0)");
      g.strokeStyle = gr;
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(x, TOP - 20);
      g.lineTo(x, bot + 10);
      g.stroke();
    });
    // bands
    const order = [...this.links.values()].sort((a, b) => a.w - b.w);
    for (const l of order) {
      if (!l._pL || !l._pR) continue;
      const L = this.nodes.get(l.L)!;
      const R = this.nodes.get(l.R)!;
      const { xa, xb, a, b } = this.ends(l);
      const [c1, c2] = this.ctrl(l, xa, xb);
      const inFocus = fr ? fr.set.has(l.L) && fr.set.has(l.R) : focus ? focus.has(l.key) : true;
      l.glow *= Math.pow(0.15, dt);
      const alpha = (inFocus ? 0.3 : 0.05) + l.glow * 0.35 * (inFocus ? 1 : 0.3);
      const gr = g.createLinearGradient(xa, 0, xb + (l.loop ? 110 : 0), 0);
      gr.addColorStop(0, hexA(L.col, alpha));
      gr.addColorStop(1, hexA(R.col, alpha));
      g.beginPath();
      g.moveTo(xa, a.y0);
      g.bezierCurveTo(c1, a.y0, c2, b.y0, xb, b.y0);
      g.lineTo(xb, b.y1);
      g.bezierCurveTo(c2, b.y1, c1, a.y1, xa, a.y1);
      g.closePath();
      g.fillStyle = gr;
      g.fill();
      g.strokeStyle = hexA(L.col, inFocus ? 0.22 + l.glow * 0.4 : 0.04);
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(xa, a.y0);
      g.bezierCurveTo(c1, a.y0, c2, b.y0, xb, b.y0);
      g.stroke();
      // ambient particles: count from the rate, direction from the share of real hops
      const want = l.rate > 0 ? Math.min(70, Math.round(3 + l.rate * 1.6)) : 0; // no traffic, no trail
      const share = (l.dirF + 1) / (l.dirF + l.dirB + 2);
      while (l.parts.length < want) l.parts.push({ t: Math.random(), lat: 0.12 + Math.random() * 0.76, v: 0.12 + Math.random() * 0.18, f: Math.random() < share });
      if (l.parts.length > want) l.parts.length = want;
      g.globalCompositeOperation = "lighter";
      for (const p of l.parts) {
        p.t += dt * p.v * (l.loop ? 0.6 : 1);
        if (p.t > 1) {
          p.t -= 1;
          p.lat = 0.12 + Math.random() * 0.76;
          p.f = Math.random() < share;
        }
        const [x, y] = this.pointOn(l, p.f ? p.t : 1 - p.t, p.lat);
        g.fillStyle = hexA(p.f ? L.col : R.col, (inFocus ? 0.55 : 0.08) * Math.sin(Math.PI * p.t));
        g.beginPath();
        g.arc(x, y, 1.4, 0, 6.283);
        g.fill();
      }
      g.globalCompositeOperation = "source-over";
    }
    // comets
    g.globalCompositeOperation = "lighter";
    const chips: [number, number, Pulse][] = [];
    const heads: typeof this.heads = [];
    for (let i = this.pulses.length - 1; i >= 0; i--) {
      const p = this.pulses[i];
      if (!p.link._pL || !p.link._pR) {
        this.pulses.splice(i, 1);
        continue;
      }
      p.t += dt * (p.link.loop ? 1.1 : 0.95);
      const u = Math.min(1, p.t);
      for (let k = 14; k >= 0; k--) {
        const s = Math.max(0, u - k * 0.022);
        const [x, y] = this.pointOn(p.link, p.fwd ? s : 1 - s, 0.5);
        const r = k === 0 ? 5 : 4.2 * (1 - k / 16);
        const gr = g.createRadialGradient(x, y, 0, x, y, r * 3);
        gr.addColorStop(0, k === 0 ? "rgba(255,255,255,1)" : hexA(p.col, 0.9 * (1 - k / 15)));
        gr.addColorStop(1, hexA(p.col, 0));
        g.fillStyle = gr;
        g.beginPath();
        g.arc(x, y, r * 3, 0, 6.283);
        g.fill();
      }
      const [hx, hy] = this.pointOn(p.link, p.fwd ? u : 1 - u, 0.5);
      chips.push([hx, hy, p]);
      heads.push({ id: p.evId, x: hx, y: hy, ev: p.ev });
      if (p.t >= 1) {
        this.pulses.splice(i, 1);
        p.done();
      }
    }
    g.globalCompositeOperation = "source-over";
    this.heads = heads;
    // nodes
    this.nodes.forEach((n) => {
      n.heat *= Math.pow(0.1, dt);
      const inFocus = fr ? fr.set.has(n.key) : focus ? [...focus].some((k) => k.split("|").includes(n.key)) : true;
      const al = inFocus ? 1 : 0.25;
      g.shadowColor = n.col;
      g.shadowBlur = 18 + n.heat * 30;
      g.fillStyle = hexA(n.col, al);
      this.roundRect(g, n.x - BW / 2, n.y - n.h / 2, BW, n.h, 6);
      g.fill();
      g.shadowBlur = 0;
      g.fillStyle = hexA("#ffffff", (0.25 + n.heat * 0.6) * al);
      this.roundRect(g, n.x - 2, n.y - n.h / 2 + 3, 4, Math.max(0, n.h - 6), 2);
      g.fill();
      const left = n.column === 0;
      g.font = `600 13.5px ${FONT}`;
      g.textAlign = left ? "right" : "left";
      g.textBaseline = "middle";
      g.fillStyle = hexA("#e8eeff", al);
      g.fillText(n.label, n.x + (left ? -16 : 16), n.y);
      const badge = this.badges[n.key];
      if (badge) {
        g.font = `600 10.5px ${MONO}`;
        g.fillStyle = hexA("#b9c6f2", al * 0.85);
        g.fillText(badge, n.x + (left ? -16 : 16), n.y + 16);
      }
      if (n.h > 46) {
        g.font = `11.5px ${FONT}`;
        g.fillStyle = hexA("#8b99c3", al);
        const pm = this.summary?.nodes[n.key] ?? 0;
        g.fillText(`${pm > 0 && pm < 1 ? pm.toLocaleString("it-IT", { maximumFractionDigits: 1 }) : Math.round(pm)} / min`, n.x + (left ? -16 : 16), n.y + (badge ? 31 : 17));
      }
      // health ring: the node's state (GET /api/flow/health), the reason in the card
      const hs = this.health[n.key]?.state;
      if (hs) {
        g.strokeStyle = HEALTH_COLORS[hs];
        g.lineWidth = hs === "ok" ? 1 : 2;
        g.setLineDash?.(hs === "sconosciuto" ? [3, 3] : []);
        this.roundRect(g, n.x - BW / 2 - 4, n.y - n.h / 2 - 4, BW + 8, n.h + 8, 9);
        g.stroke();
        g.setLineDash?.([]);
      }
    });
    // flash rings
    for (let i = this.flashes.length - 1; i >= 0; i--) {
      const f = this.flashes[i];
      f.t += dt * 1.6;
      const n = this.nodes.get(f.key);
      if (n) {
        const r = 10 + f.t * 40;
        g.strokeStyle = hexA(f.bad ? ERROR_COLOR : f.col, Math.max(0, 0.8 * (1 - f.t)));
        g.lineWidth = f.bad ? 3 : 2;
        g.beginPath();
        g.ellipse(n.x, n.y, r * 0.7, Math.max(r, n.h / 2 + r * 0.5), 0, 0, 6.283);
        g.stroke();
      }
      if (f.t >= 1 || !n) this.flashes.splice(i, 1);
    }
    // tool chips over the comets
    g.font = `600 11px ${MONO}`;
    g.textAlign = "left";
    g.textBaseline = "middle";
    for (const [hx, hy, p] of chips) {
      if (!p.label) continue;
      const w = g.measureText(p.label).width + 14;
      const x = Math.min(hx + 10, this.W - w - 2);
      const y = hy - 24;
      this.roundRect(g, x, y - 9, w, 18, 9);
      g.fillStyle = "rgba(8,10,22,.8)";
      g.fill();
      g.strokeStyle = "rgba(255,255,255,.2)";
      g.lineWidth = 1;
      g.stroke();
      g.fillStyle = p.col;
      g.fillText(p.label, x + 7, y + 0.5);
    }
    this.drawTimeline();
  }

  /** the replay clock: moves with the speed, never ahead of the replay delay; when it
   * falls behind by more than delay + spread (a pause, a hidden tab, a slow frame) it snaps
   * back to the delay: what is too old joins the history, what was just behind is spread
   * over SPREAD_MS (no comet storm); due events start their comets */
  private advanceClock(raw: number): void {
    if (this.playT === null || this.paused) return;
    const r = this.replay;
    if (r) {
      const limit = Math.min(r.to, r.horizon);
      if (this.playT < limit) this.playT = Math.min(limit, this.playT + Math.round(raw * 1000) * r.speed);
      let n = 0;
      while (r.next < this.recent.length && this.recent[r.next].t <= this.playT) {
        const ev = this.recent[r.next++].ev;
        if (n++ < REPLAY_BURST && this.valid(ev)) this.run(ev);
        else this.launchCb?.(ev); // diary and timeline only
      }
      return;
    }
    const live = this.liveNow();
    const target = live - REPLAY_DELAY_MS;
    this.playT = Math.min(this.playT + raw * 1000 * this.speed, target);
    if (target - this.playT > SPREAD_MS) {
      this.playT = target;
      const behind = this.queue.filter((q) => q.at < target);
      const ahead = this.queue.filter((q) => q.at >= target);
      const recent = behind.filter((q) => Date.parse(q.ev.ts) >= target - LATE_MS);
      behind.filter((q) => Date.parse(q.ev.ts) < target - LATE_MS).forEach((q) => this.launchCb?.(q.ev));
      recent.forEach((q, i) => (q.at = target + (i / recent.length) * SPREAD_MS));
      this.queue = [...recent, ...ahead].sort((a, b) => a.at - b.at);
    }
    while (this.queue.length && this.queue[0].at <= this.playT) this.run(this.queue.shift()!.ev);
  }

  /** once a second: drop old history, prevailing directions in the window, focus */
  private stats(now: number): void {
    this.lastStats = now;
    const play = this.playT ?? this.liveNow();
    if (!this.replay) {
      const keepFrom = this.liveNow() - KEEP_MS; // the live clock: trimmed during a pause too
      let cut = 0;
      while (cut < this.recent.length && this.recent[cut].t < keepFrom) cut++;
      if (cut) this.recent.splice(0, cut);
    }
    this.links.forEach((l) => {
      l.dirF = 0;
      l.dirB = 0;
    });
    const from = play - this.win * 60_000;
    for (let i = this.upper(from - 1), end = this.upper(play); i < end; i++) {
      const { t, ev } = this.recent[i];
      if (t < from) continue;
      for (let i = 0; i < ev.path.length - 1; i++) {
        const h = this.hopLink(ev.path[i], ev.path[i + 1]);
        if (!h) continue;
        if (h.fwd) h.link.dirF++;
        else h.link.dirB++;
      }
    }
    this.refreshFocus();
  }

  private drawHeads(g: CanvasRenderingContext2D, cols: RNode[][], m: { left: number; right: number }): void {
    g.textAlign = "center";
    g.textBaseline = "alphabetic";
    const ls = "letterSpacing" in g;
    cols.forEach((c, i) => {
      if (!c.length) return;
      const x = this.colX(i, m);
      g.font = `700 11px ${FONT}`;
      if (ls) (g as unknown as { letterSpacing: string }).letterSpacing = "2px";
      g.fillStyle = "#5f6f9e";
      g.fillText(COLUMN_HEADS[i].title, x, 16);
      if (ls) (g as unknown as { letterSpacing: string }).letterSpacing = "0px";
      g.font = `500 11px ${FONT}`;
      g.fillStyle = "#4c5a85";
      g.fillText(COLUMN_HEADS[i].sub, x, 31);
    });
  }

  private drawTimeline(): void {
    const tg = this.tg;
    const TW = this.TW;
    const TH = this.TH;
    if (!tg || TW <= 0 || TH <= 0) return;
    const play = this.playT ?? this.liveNow() - REPLAY_DELAY_MS;
    const series = Array.from({ length: TIMELINE_S }, () => ({ agent: 0, rules: 0, system: 0 }));
    for (let i = this.upper(play) - 1; i >= 0; i--) {
      const { t, ev } = this.recent[i];
      const ago = Math.floor((play - t) / 1000);
      if (ago >= TIMELINE_S) break;
      series[TIMELINE_S - 1 - ago][originOf(ev)]++;
    }
    tg.clearRect(0, 0, TW, TH);
    const max = Math.max(3, ...series.map((s) => s.agent + s.rules + s.system));
    const dx = TW / (TIMELINE_S - 1);
    for (let y = 0; y <= 3; y++) {
      tg.strokeStyle = "rgba(255,255,255,.05)";
      tg.lineWidth = 1;
      tg.beginPath();
      tg.moveTo(0, TH - (y / 3) * TH + 0.5);
      tg.lineTo(TW, TH - (y / 3) * TH + 0.5);
      tg.stroke();
    }
    const base = series.map(() => 0);
    for (const o of ORIGINS) {
      tg.beginPath();
      series.forEach((s, i) => {
        const y = TH - ((base[i] + s[o.key]) / max) * (TH - 6);
        if (i) tg.lineTo(i * dx, y);
        else tg.moveTo(0, y);
      });
      for (let i = series.length - 1; i >= 0; i--) tg.lineTo(i * dx, TH - (base[i] / max) * (TH - 6));
      tg.closePath();
      const gr = tg.createLinearGradient(0, 0, 0, TH);
      gr.addColorStop(0, `${o.color}cc`);
      gr.addColorStop(1, `${o.color}22`);
      tg.fillStyle = gr;
      tg.fill();
      series.forEach((s, i) => (base[i] += s[o.key]));
    }
    tg.fillStyle = "#6f80b3";
    tg.font = `11px ${FONT}`;
    tg.textBaseline = "alphabetic";
    tg.textAlign = "right";
    tg.fillText(this.replay ? "ora mostrata" : "adesso", TW - 2, 12);
    tg.textAlign = "left";
    tg.fillText("−2 min", 2, 12);
  }

  private roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    g.beginPath();
    g.moveTo(x + r, y);
    g.arcTo(x + w, y, x + w, y + h, r);
    g.arcTo(x + w, y + h, x, y + h, r);
    g.arcTo(x, y + h, x, y, r);
    g.arcTo(x, y, x + w, y, r);
    g.closePath();
  }
}
