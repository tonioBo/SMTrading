import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  useFlowAgents,
  useFlowEvents,
  useFlowHealth,
  useFlowMap,
  useFlowReplay,
  useFlowSources,
  useFlowSummary,
  useFlowTools,
} from "@/api/hooks/flow";
import type { FlowAgents, FlowEvent, FlowHealth, FlowNode, FlowSummary, FlowTools, FlowWindow } from "@/api/types";
import { Lens } from "@/components/flow/Lens";
import { ReplayBar } from "@/components/flow/ReplayBar";
import { HEALTH_COLORS, kindColor, ORIGINS, RiverCanvas, type HoverTarget } from "@/components/flow/RiverCanvas";
import { LENS_NODES, storyTarget, SummaryTally, type LensKind, windowLabel, windowMinutes, type Range, type ReplaySpeed, type StoryTarget } from "@/components/flow/replay";
import { StoryPanel } from "@/components/flow/StoryPanel";
import { BTN, BTN_ON, clock, GLASS, H4, num } from "@/components/flow/styles";
import { cn } from "@/lib/utils";

/** «Fiume dei flussi»: who talks to whom in SMTrading, on the real traffic (read every 2 s).
 * The stage keeps the approved sketch's dark look in both portal themes.
 *
 * v2, all on request (the general view stays as v1 until something is opened): the
 * «moviola» (a past window replayed), an order's story (a comet or a diary line), health
 * rings and agents' spend on the nodes, the lens on «MCP SMTrading» and «Prezzi», the
 * width window «oggi». */

const STAGE_BG =
  "radial-gradient(1100px 600px at 18% 40%, rgba(90,80,255,.16), transparent 60%), radial-gradient(900px 600px at 55% 55%, rgba(20,190,160,.12), transparent 60%), radial-gradient(900px 600px at 88% 45%, rgba(255,150,50,.10), transparent 60%), linear-gradient(180deg,#080c1b,#05070f)";
const DIARY_MAX = 8;
const SPEEDS = [0.5, 1, 2] as const;
const WINDOWS: FlowWindow[] = [1, 5, 15, "today"];
/** Paperclip runs older than this are not drawn live (the river keeps 15 minutes) */
const RUNS_LIVE_MS = 15 * 60_000;
const UNAVAILABLE = "dati di Paperclip non disponibili";



/** the name a diary line shows for an event (the tool, for an MCP call) */
function toolOf(e: FlowEvent): string {
  if (e.kind === "order_sent") return e.hops[1] ?? e.hops[0] ?? e.kind;
  return e.hops[0] ?? e.kind;
}

interface DiaryLine {
  id: string;
  /** the story it opens (an order event), null for the others */
  target: StoryTarget | null;
  time: string;
  who: string;
  color: string;
  tool: string;
  error: boolean;
  ms: number | null;
}

function Toggle({ on, onClick, children, label }: { on: boolean; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button type="button" className={cn(BTN, on && BTN_ON)} aria-pressed={on} aria-label={label} onClick={onClick}>
      {children}
    </button>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="whitespace-nowrap text-[11.5px] text-[#8b99c3]">
      <b className="num block text-[19px] font-semibold leading-tight text-[#e8eeff]">{value}</b>
      {label}
    </div>
  );
}

export default function FlowRiver() {
  const [win, setWin] = useState<FlowWindow>(1);
  const [paused, setPaused] = useState(false);
  const [speed, setSpeed] = useState<0.5 | 1 | 2>(1);
  const [follow, setFollow] = useState(false);
  const [followed, setFollowed] = useState<FlowEvent | null>(null);
  const [hover, setHover] = useState<HoverTarget | null>(null);
  const [diary, setDiary] = useState<DiaryLine[]>([]);
  // v2
  const [replayOpen, setReplayOpen] = useState(false);
  const [range, setRange] = useState<Range | null>(null);
  const [playing, setPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState<ReplaySpeed>(60);
  const [shown, setShown] = useState<number | null>(null);
  const [story, setStory] = useState<StoryTarget | null>(null);
  const [lens, setLens] = useState<LensKind | null>(null);
  /** the replayed window's widths and event count, tallied page by page */
  const [replayed, setReplayed] = useState<{ summary: FlowSummary; count: number } | null>(null);
  const fed = useRef<{ events: number; runs: Set<string>; all: FlowEvent[]; tally: SummaryTally | null; links: [string, string][] | undefined }>({
    events: 0,
    runs: new Set(),
    all: [],
    tally: null,
    links: undefined,
  });
  const inReplay = range !== null;

  const map = useFlowMap();
  const summary = useFlowSummary(win);
  const { events, now, stale } = useFlowEvents();
  const agents = useFlowAgents();
  const health = useFlowHealth();
  const tools = useFlowTools(win, lens === "mcp" || !!hover?.link);
  const sources = useFlowSources(lens === "px");
  const replay = useFlowReplay(range);

  const stageRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const riverRef = useRef<HTMLCanvasElement>(null);
  const timelineRef = useRef<HTMLCanvasElement>(null);
  const river = useRef<RiverCanvas | null>(null);
  const lastPass = useRef(new Map<string, { time: string; tool: string }>());

  const nodes = useMemo(() => new Map<string, FlowNode>((map.data?.nodes ?? []).map((n) => [n.key, n])), [map.data]);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  /** the lens' comets: every event the river launches */
  const listeners = useRef(new Set<(e: FlowEvent) => void>());
  const subscribe = useCallback((cb: (e: FlowEvent) => void) => {
    listeners.current.add(cb);
    return () => {
      listeners.current.delete(cb);
    };
  }, []);

  useEffect(() => {
    const r = new RiverCanvas(riverRef.current!, timelineRef.current!);
    river.current = r;
    r.onHover(setHover);
    r.onFollow(setFollowed);
    r.onSelect((e) => {
      const t = storyTarget(e);
      if (t) setStory(t);
    });
    r.onNodeClick((k) => {
      if (!LENS_NODES.includes(k)) return false;
      setLens(k as LensKind);
      setHover(null);
      return true;
    });
    r.onLaunch((e) => {
      const time = clock(e.ts);
      e.path.forEach((k, i) => lastPass.current.set(k, { time, tool: e.kind === "mcp" ? e.hops[0] : (e.hops[Math.max(0, i - 1)] ?? toolOf(e)) }));
      const who = nodesRef.current.get(e.who);
      const runOf = e.kind === "run" ? nodesRef.current.get(e.path[1] ?? "")?.label : undefined;
      const line: DiaryLine = {
        id: e.id,
        target: storyTarget(e),
        time,
        who: who?.label ?? e.who,
        color: kindColor(who?.kind ?? "core"),
        tool: e.kind === "run" ? `sveglia ${runOf ?? e.path[1] ?? "un agente"} · ${e.hops[0] ?? "risveglio"}` : toolOf(e),
        error: e.outcome === "error",
        ms: e.latency_ms,
      };
      setDiary((d) => [line, ...d.filter((x) => x.id !== e.id)].slice(0, DIARY_MAX));
      listeners.current.forEach((l) => l(e));
    });
    return () => {
      r.destroy();
      river.current = null;
    };
  }, []);

  useEffect(() => {
    if (map.data) river.current?.setMap(map.data);
  }, [map.data]);

  // the replay: a past window on its own clock; the pages are merged as they arrive, with
  // the Paperclip runs of the window (they come only from /api/flow/agents)
  useEffect(() => {
    setDiary([]);
    setStory(null);
    fed.current = { events: 0, runs: new Set(), all: [], tally: null, links: undefined };
    setReplayed(null);
    if (!range) {
      river.current?.stopReplay();
      setShown(null);
      return;
    }
    river.current?.startReplay(range.from, range.to);
    setShown(range.from);
    setPlaying(true);
  }, [range]);

  const runs = agents.data?.runs;
  const mapLinks = map.data?.links;
  // only what is new goes to the river and the tally: the page just read, and the
  // window's Paperclip runs not handed over yet
  useEffect(() => {
    if (!range) return;
    const f = fed.current;
    const fresh = replay.events.length > f.events ? replay.events.slice(f.events) : [];
    f.events = Math.max(f.events, replay.events.length);
    for (const e of runs ?? []) {
      const t = Date.parse(e.ts);
      if (t < range.from || t > range.to || f.runs.has(e.id)) continue;
      f.runs.add(e.id);
      fresh.push(e);
    }
    if (fresh.length) river.current?.addReplay(fresh);
    f.all = f.all.concat(fresh);
    // a new map (or the first, after the first page): the tally starts again on it
    const rebuild = f.tally !== null && f.links !== mapLinks;
    if (!f.tally || rebuild) {
      f.tally = new SummaryTally(mapLinks ?? []);
      f.links = mapLinks;
      f.tally.add(f.all);
    } else f.tally.add(fresh);
    if (fresh.length || rebuild) setReplayed({ summary: f.tally.summary((range.to - range.from) / 60_000), count: f.all.length });
    // the pages come in time order: the clock waits at the last one loaded
    const last = replay.events.length ? Date.parse(replay.events[replay.events.length - 1].ts) : range.from;
    river.current?.setReplayHorizon(replay.done ? range.to : last);
  }, [range, replay.events, replay.done, runs, mapLinks]);

  useEffect(() => river.current?.setReplaySpeed(replaySpeed), [replaySpeed, range]);

  const replaySummary = range ? (replayed?.summary ?? null) : null;

  useEffect(() => {
    if (replaySummary && range) {
      river.current?.setWindow((range.to - range.from) / 60_000);
      river.current?.setSummary(replaySummary);
      return;
    }
    if (!summary.data) return;
    river.current?.setWindow(windowMinutes(win, Date.now()));
    river.current?.setSummary(summary.data);
  }, [summary.data, win, replaySummary, range]);

  // the time shown, and the end of the window
  useEffect(() => {
    if (!range) return;
    const id = setInterval(() => {
      const r = river.current;
      if (!r) return;
      setShown(r.replayClock());
      if (r.replayEnded()) setPlaying(false);
    }, 250);
    return () => clearInterval(id);
  }, [range]);

  useEffect(() => {
    if (now && !inReplay) river.current?.push(events, now);
  }, [events, now, inReplay]);

  // the Paperclip runs as impulses Paperclip → agent (merged by id: the river drops the
  // ones it has seen); the old ones are history only
  useEffect(() => {
    if (inReplay || !runs?.length) return;
    const since = Date.now() - RUNS_LIVE_MS;
    const recent = runs.filter((e) => Date.parse(e.ts) >= since);
    if (recent.length) river.current?.push(recent);
  }, [runs, inReplay]);

  useEffect(() => {
    if (health.data) river.current?.setHealth(health.data.nodes);
  }, [health.data]);


  useEffect(() => river.current?.setPaused(inReplay ? !playing : paused), [paused, playing, inReplay]);
  useEffect(() => river.current?.setSpeed(speed), [speed]);
  useEffect(() => {
    river.current?.setFollow(follow);
    if (!follow) setFollowed(null);
  }, [follow]);

  const closeReplay = () => {
    setReplayOpen(false);
    setRange(null);
    setPlaying(false);
  };
  const closeLens = useCallback(() => setLens(null), []);
  const session = map.data?.last_session;
  const lastSession = useMemo(() => {
    if (!session) return null;
    const from = Date.parse(session.from);
    const to = Date.parse(session.to);
    return Number.isFinite(from) && Number.isFinite(to) ? { from, to } : null;
  }, [session]);

  const shownSummary = inReplay ? replaySummary : summary.data;
  const head = shownSummary?.head;
  const links = shownSummary?.links ?? {};
  const totalCalls = Object.values(links).reduce((a, l) => a + l.calls, 0);
  const quiet = inReplay ? replay.done && !replayed?.count : summary.data !== undefined && totalCalls === 0;
  const latency = head?.mcp_latency_ms != null ? `${num(head.mcp_latency_ms, 0)} ms` : "—";
  const label = (k: string) => nodes.get(k)?.label ?? k;
  const winText = inReplay ? "nell'intervallo riprodotto" : win === "today" ? "oggi" : `ultimi ${win} min`;
  const paperclipDown = agents.data && !agents.data.available ? (agents.data.note ?? `Paperclip non raggiungibile: ${UNAVAILABLE}`) : null;

  const ariaLabel = head
    ? `Fiume dei flussi${inReplay ? " in moviola" : ""}, ${winText}: ${num(head.calls_per_min)} chiamate al minuto, ${head.orders_today} ${inReplay ? "ordini degli agenti" : "ordini oggi"}, ` +
      `${head.errors_window} errori, tempo medio ${latency}; ${nodes.size} nodi e ${Object.keys(links).length} correnti con traffico.`
    : "Fiume dei flussi: caricamento dei dati";

  return (
    <div
      ref={stageRef}
      className="relative flex h-[calc(100dvh-6.5rem)] min-h-[680px] flex-col gap-3 overflow-hidden rounded-2xl p-3 text-[13px] leading-snug text-[#e8eeff] md:-m-2"
      style={{ background: STAGE_BG }}
    >
      {/* header: title, one-liner, counters, controls */}
      <div className={cn(GLASS, "flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3")}>
        <h1 className="m-0 whitespace-nowrap text-[19px] font-semibold">Fiume dei flussi</h1>
        <p className="m-0 max-w-[430px] text-[12.5px] leading-snug text-[#8b99c3]">
          Da sinistra a destra, dagli agenti ai broker. La larghezza di ogni corrente è quante chiamate ci passano; le scie mostrano in che verso vanno.
        </p>
        <div className="flex-1" />
        <div role="group" aria-label="Contatori" className="flex gap-5">
          <Stat value={head ? num(head.calls_per_min) : "—"} label="chiamate / min" />
          <Stat value={head ? String(head.orders_today) : "—"} label={inReplay ? "ordini degli agenti" : "ordini oggi"} />
          <Stat value={head ? String(head.errors_window) : "—"} label={inReplay ? "errori nell'intervallo" : win === "today" ? "errori oggi" : `errori in ${win} min`} />
          <Stat value={latency} label="tempo medio" />
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11.5px] text-[#8b99c3]">
          <div className="flex gap-1">
            {!inReplay && (
              <Toggle on={paused} onClick={() => setPaused((p) => !p)}>
                {paused ? "▶ Riprendi" : "⏸ Pausa"}
              </Toggle>
            )}
            <Toggle on={follow} onClick={() => setFollow((f) => !f)}>
              ◎ Segui un ordine
            </Toggle>
            <Toggle on={replayOpen} onClick={() => (replayOpen ? closeReplay() : setReplayOpen(true))}>
              ⏮ Moviola
            </Toggle>
          </div>
          {!inReplay && (
            <div role="group" aria-label="Velocità" className="flex items-center gap-1">
              velocità
              {SPEEDS.map((s) => (
                <Toggle key={s} on={speed === s} onClick={() => setSpeed(s)} label={`velocità ${s === 0.5 ? "metà" : `${s}×`}`}>
                  {s === 0.5 ? "½×" : `${s}×`}
                </Toggle>
              ))}
            </div>
          )}
          {inReplay ? (
            <span>larghezza sull&apos;intervallo riprodotto</span>
          ) : (
            <div role="group" aria-label="Larghezza su" className="flex items-center gap-1">
              larghezza su
              {WINDOWS.map((w) => (
                <Toggle key={w} on={win === w} onClick={() => setWin(w)}>
                  {windowLabel(w)}
                </Toggle>
              ))}
            </div>
          )}
        </div>
      </div>

      {replayOpen && (
        <ReplayBar
          range={range}
          shown={shown}
          playing={playing}
          speed={replaySpeed}
          loading={{ pages: replay.pages, events: replayed?.count ?? 0, done: replay.done, capped: replay.capped, error: replay.error }}
          now={Date.now}
          lastSession={lastSession}
          onRange={setRange}
          onPlay={(b) => {
            const r = river.current;
            if (b && r?.replayEnded() && range) r.seek(range.from); // from the start again
            setPlaying(b);
          }}
          onSpeed={setReplaySpeed}
          onSeek={(t) => {
            river.current?.seek(t);
            setShown(t);
          }}
          onClose={closeReplay}
        />
      )}

      {/* the river */}
      <div ref={wrapRef} className="relative min-h-[360px] flex-1">
        <canvas ref={riverRef} role="img" aria-label={ariaLabel} className="absolute left-0 top-0" />
        <div className="pointer-events-none absolute inset-x-0 top-12 flex flex-col items-center gap-2">
          {stale && (
            <div role="status" className={cn(GLASS, "border-[rgba(255,179,71,.4)] px-3 py-1.5 text-xs text-[#ffd08f]")}>
              dati non aggiornati, nuovo tentativo…
            </div>
          )}
          {quiet && (
            <div role="status" className={cn(GLASS, "px-3 py-1.5 text-xs text-[#a9b5dc]")}>
              {inReplay ? "Nessuna chiamata nell'intervallo" : win === "today" ? "Nessuna chiamata oggi" : `Nessuna chiamata negli ultimi ${win} minuti`}
            </div>
          )}
          {paperclipDown && (
            <div role="status" className={cn(GLASS, "px-3 py-1.5 text-xs text-[#a9b5dc]")}>
              {paperclipDown}
            </div>
          )}
        </div>
        {follow && (
          <div className={cn(GLASS, "pointer-events-none absolute bottom-3 left-1/2 w-max max-w-[min(760px,92%)] -translate-x-1/2 px-4 py-2.5 text-center text-[13.5px]")}>
            <Story event={followed} label={label} />
          </div>
        )}
        {hover && !lens && (
          <HoverCard
            hover={hover}
            host={wrapRef.current}
            nodes={nodes}
            label={label}
            win={inReplay ? null : win}
            summary={shownSummary}
            river={river.current}
            lastPass={lastPass.current}
            mapLinks={map.data?.links ?? []}
            health={health.data}
            agents={agents.data}
            tools={tools.data}
          />
        )}
        {/* the lenses from the keyboard: shown when focused */}
        <div className="absolute left-2 top-2 z-10 flex gap-1">
          {LENS_NODES.filter((k) => nodes.has(k)).map((k) => (
            <button key={k} type="button" className={cn(BTN, "sr-only focus:not-sr-only")} onClick={() => setLens(k as LensKind)}>
              Apri la lente su {label(k)}
            </button>
          ))}
        </div>
        {story && <StoryPanel target={story} onClose={() => setStory(null)} onOpen={setStory} />}
        {lens && (
          <Lens
            kind={lens}
            title={label(lens)}
            win={win}
            tools={tools.data}
            sources={sources.data}
            live={inReplay}
            subscribe={subscribe}
            onClose={closeLens}
          />
        )}
      </div>

      {/* 2-minute timeline and live diary */}
      <div className={cn(GLASS, "flex h-[150px] shrink-0 gap-4 px-4 py-3")}>
        <div className="flex min-w-0 flex-1 flex-col">
          <h4 className={H4}>{inReplay ? "2 minuti prima dell'ora mostrata" : "Ultimi 2 minuti"} · passaggi al secondo per chi li fa partire</h4>
          <div className="relative flex-1">
            <canvas ref={timelineRef} aria-hidden className="absolute left-0 top-0" />
          </div>
          <div className="mt-1 flex gap-3.5 text-[11.5px] text-[#8b99c3]">
            {ORIGINS.map((o) => (
              <span key={o.key}>
                <i className="mr-1.5 inline-block h-[9px] w-[9px] rounded-sm align-[-1px]" style={{ background: o.color }} />
                {o.label}
              </span>
            ))}
          </div>
        </div>
        <div className="hidden w-[400px] shrink-0 sm:block">
          <h4 className={H4} id="flow-diary">
            {inReplay ? "Chiamate in moviola" : "Chiamate in diretta"}
          </h4>
          <div role="log" aria-labelledby="flow-diary" className="h-[112px] overflow-hidden font-mono text-xs leading-[1.62] text-[#8b99c3]">
            {diary.length === 0 && <div className="italic">in attesa del primo passaggio…</div>}
            {diary.map((d) => {
              const line = (
                <>
                  {d.time}{" "}
                  <b className="font-semibold" style={{ color: d.color }}>
                    {d.who}
                  </b>{" "}
                  <span className="text-[#cdb8ff]">{d.tool}</span>{" "}
                  {d.error ? <span className="text-[#ff6f8c]">✕ errore</span> : d.ms != null ? `· ${d.ms} ms` : null}
                </>
              );
              const cls = "animate-in fade-in slide-in-from-right-2 truncate duration-300";
              return d.target ? (
                <button
                  key={d.id}
                  type="button"
                  aria-label={`storia: ${d.who} ${d.tool}`}
                  className={cn(cls, "block w-full cursor-pointer text-left hover:text-[#e8eeff] hover:underline focus-visible:outline focus-visible:outline-1 focus-visible:outline-[#34e0c4]")}
                  onClick={() => setStory(d.target)}
                >
                  {line}
                </button>
              ) : (
                <div key={d.id} className={cls}>
                  {line}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function Story({ event, label }: { event: FlowEvent | null; label: (k: string) => string }) {
  if (!event) return <>In attesa del prossimo ordine: appena ne parte uno, il suo percorso si illumina passo per passo.</>;
  // every name from the map, along the event's own path
  const on = event.symbol ? ` su ${event.symbol}` : "";
  const [p0, p1, p2, p3] = event.path.map(label);
  if (event.kind === "order_sent") {
    return (
      <>
        <b className="text-[#9ff3df]">Un ordine verso il broker</b>
        {on}: {p0} lo passa a {p1}, che lo manda a {p2}; l&apos;esecuzione tornerà indietro dalla stessa strada.
      </>
    );
  }
  return (
    <>
      <b className="text-[#9ff3df]">Un ordine</b>
      {on}: {p0} chiede a SMTrading di eseguirlo ({event.hops[0]}): {p1} lo porta a {p2}
      {p3 ? `, poi ${p3} controlla il rischio e prenota la cassa prima di mandarlo al broker.` : "."}
    </>
  );
}

interface HoverProps {
  hover: HoverTarget;
  host: HTMLElement | null;
  nodes: Map<string, FlowNode>;
  label: (k: string) => string;
  /** the width window, null in replay (the replayed window) */
  win: FlowWindow | null;
  summary: ReturnType<typeof useFlowSummary>["data"] | null;
  river: RiverCanvas | null;
  lastPass: Map<string, { time: string; tool: string }>;
  mapLinks: [string, string][];
  health: FlowHealth | undefined;
  agents: FlowAgents | undefined;
  tools: FlowTools | undefined;
}

function HoverCard({ hover, host, nodes, label, win, summary, river, lastPass, mapLinks, health, agents, tools }: HoverProps) {
  const minutes = win === null ? null : windowMinutes(win, Date.now());
  const inWin = win === null ? "nell'intervallo" : win === "today" ? "oggi" : `in ${win} min`;
  const w = host?.clientWidth ?? 1200;
  const h = host?.clientHeight ?? 800;
  const style = { left: Math.max(0, Math.min(w - 350, hover.x + 18)), top: Math.max(0, Math.min(h - 190, hover.y + 14)) };
  let body: ReactNode = null;
  if (hover.node) {
    const n = nodes.get(hover.node);
    if (!n) return null;
    const pass = lastPass.get(n.key);
    const linked = mapLinks.filter(([a, b]) => a === n.key || b === n.key).length;
    const h = health?.nodes[n.key];
    const agent = agents?.agents.find((a) => a.key === n.key);
    body = (
      <>
        <div className="mb-1 text-sm font-semibold" style={{ color: kindColor(n.kind) }}>
          {n.label}
        </div>
        <div className="mb-1.5 text-[#a9b5dc]">{n.description}</div>
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-xs text-[#8b99c3]">
          <dt>passaggi / min</dt>
          <dd className="num font-semibold text-[#e8eeff]">{num(summary?.nodes[n.key] ?? 0)}</dd>
          <dt>correnti collegate</dt>
          <dd className="num font-semibold text-[#e8eeff]">{linked}</dd>
          <dt>ultimo passaggio</dt>
          <dd className="font-semibold text-[#e8eeff]">{pass ? `${pass.time} · ${pass.tool}` : "—"}</dd>
          {agent && agents?.available && (
            <>
              {agent.status_it && (
                <>
                  <dt>in Paperclip</dt>
                  <dd className="font-semibold text-[#e8eeff]">{agent.status_it}</dd>
                </>
              )}
            </>
          )}
        </dl>
        {agent && !agents?.available && <div className="mt-1.5 text-xs italic text-[#8b99c3]">{UNAVAILABLE}</div>}
        {agent && agents?.available && agent.runs.length > 0 && (
          <div className="mt-1.5 text-xs text-[#8b99c3]">
            <div className="mb-0.5 font-semibold text-[#a9b5dc]">ultime esecuzioni</div>
            {agent.runs.slice(0, 3).map((r) => (
              <div key={r.id} className={r.outcome === "errore" ? "text-[#ff6f8c]" : undefined}>
                {clock(r.ts)} · {r.reason_it} · {r.outcome}
              </div>
            ))}
          </div>
        )}
        {h && (
          <div className="mt-1.5 text-xs font-semibold" style={{ color: h.state === "ok" ? "#34e0c4" : HEALTH_COLORS[h.state] }}>
            {h.state} · <span className="font-normal text-[#a9b5dc]">{h.reason}</span>
          </div>
        )}
      </>
    );
  } else if (hover.link) {
    const [a, b] = hover.link.split("|");
    const s = summary?.links[hover.link];
    const dir = river?.linkDirection(hover.link);
    const all = tools?.links[hover.link];
    const top = s?.top_tools.map((t) => `${t.tool} (${t.calls})`).join(", ");
    body = (
      <>
        <div className="mb-1 text-sm font-semibold">
          {label(a)} ↔ {label(b)}
        </div>
        <div className="mb-1.5 text-[#a9b5dc]">
          {dir ? `verso prevalente: ${label(dir[0])} → ${label(dir[1])}` : `nessun passaggio ${win === null ? "nell'intervallo" : win === "today" ? "oggi" : `negli ultimi ${win} min`}`}
        </div>
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-xs text-[#8b99c3]">
          {minutes !== null && (
            <>
              <dt>chiamate / min</dt>
              <dd className="num font-semibold text-[#e8eeff]">{num((s?.calls ?? 0) / minutes)}</dd>
            </>
          )}
          {minutes === null && (
            <>
              <dt>chiamate {inWin}</dt>
              <dd className="num font-semibold text-[#e8eeff]">{s?.calls ?? 0}</dd>
            </>
          )}
          {!all?.length && (
            <>
              <dt>più frequenti</dt>
              <dd className="max-w-[180px] text-right font-semibold text-[#e8eeff]">{top || "—"}</dd>
            </>
          )}
          <dt>errori {inWin}</dt>
          <dd className="num font-semibold text-[#e8eeff]">{s?.errors ?? 0}</dd>
        </dl>
        {all && all.length > 0 && (
          <dl className="mt-1.5 grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 border-t border-white/10 pt-1.5 text-xs text-[#8b99c3]">
            {win === null && <dt className="col-span-2 italic">strumenti in diretta (non della moviola)</dt>}
            {all.slice(0, 12).map((t) => (
              <div key={t.tool} className="contents">
                <dt className="truncate font-mono text-[#cdb8ff]">{t.tool}</dt>
                <dd className="num text-right font-semibold text-[#e8eeff]">
                  {num(t.calls, 0)}
                  {t.avg_ms != null ? ` · ${num(t.avg_ms, 0)} ms` : ""}
                  {t.errors ? ` · ${t.errors} ✕` : ""}
                </dd>
              </div>
            ))}
            {all.length > 12 && <dt className="col-span-2 italic">e altri {all.length - 12}</dt>}
          </dl>
        )}
      </>
    );
  }
  return (
    <div className={cn(GLASS, "pointer-events-none absolute z-10 min-w-[220px] max-w-[320px] px-3 py-2.5")} style={style}>
      {body}
    </div>
  );
}
