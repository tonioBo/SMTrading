import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useFlowEvents, useFlowMap, useFlowSummary } from "@/api/hooks/flow";
import type { FlowEvent, FlowNode, FlowWindow } from "@/api/types";
import { kindColor, ORIGINS, RiverCanvas, type HoverTarget } from "@/components/flow/RiverCanvas";
import { cn } from "@/lib/utils";

/** «Fiume dei flussi»: who talks to whom in SMTrading, on the real traffic (read every 2 s).
 * The stage keeps the approved sketch's dark look in both portal themes. */

const GLASS =
  "rounded-2xl border border-[rgba(150,170,255,.15)] bg-[rgba(9,12,26,.6)] shadow-[0_16px_46px_rgba(0,0,0,.4)] backdrop-blur-md";
const BTN =
  "whitespace-nowrap rounded-[9px] border border-white/15 bg-white/[.06] px-2.5 py-1.5 text-xs font-semibold text-[#e8eeff] hover:bg-white/[.12] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#34e0c4]";
const BTN_ON = "border-[#34e0c4] bg-[rgba(52,224,196,.18)] text-[#bff7ec] hover:bg-[rgba(52,224,196,.24)]";
const STAGE_BG =
  "radial-gradient(1100px 600px at 18% 40%, rgba(90,80,255,.16), transparent 60%), radial-gradient(900px 600px at 55% 55%, rgba(20,190,160,.12), transparent 60%), radial-gradient(900px 600px at 88% 45%, rgba(255,150,50,.10), transparent 60%), linear-gradient(180deg,#080c1b,#05070f)";
const H4 = "mb-1.5 text-[10.5px] font-semibold uppercase tracking-[.16em] text-[#6f80b3]";
const DIARY_MAX = 8;
const SPEEDS = [0.5, 1, 2] as const;
const WINDOWS: FlowWindow[] = [1, 5, 15];

const num = (n: number, digits = 1) => n.toLocaleString("it-IT", { maximumFractionDigits: digits });
const clock = (ts: string) => new Date(ts).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

/** the name a diary line shows for an event (the tool, for an MCP call) */
function toolOf(e: FlowEvent): string {
  if (e.kind === "order_sent") return e.hops[1] ?? e.hops[0] ?? e.kind;
  return e.hops[0] ?? e.kind;
}

interface DiaryLine {
  id: string;
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

  const map = useFlowMap();
  const summary = useFlowSummary(win);
  const { events, now, stale } = useFlowEvents();

  const stageRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const riverRef = useRef<HTMLCanvasElement>(null);
  const timelineRef = useRef<HTMLCanvasElement>(null);
  const river = useRef<RiverCanvas | null>(null);
  const lastPass = useRef(new Map<string, { time: string; tool: string }>());

  const nodes = useMemo(() => new Map<string, FlowNode>((map.data?.nodes ?? []).map((n) => [n.key, n])), [map.data]);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;

  useEffect(() => {
    const r = new RiverCanvas(riverRef.current!, timelineRef.current!);
    river.current = r;
    r.onHover(setHover);
    r.onFollow(setFollowed);
    r.onLaunch((e) => {
      const time = clock(e.ts);
      e.path.forEach((k, i) => lastPass.current.set(k, { time, tool: e.kind === "mcp" ? e.hops[0] : (e.hops[Math.max(0, i - 1)] ?? toolOf(e)) }));
      const who = nodesRef.current.get(e.who);
      const line: DiaryLine = {
        id: e.id,
        time,
        who: who?.label ?? e.who,
        color: kindColor(who?.kind ?? "core"),
        tool: toolOf(e),
        error: e.outcome === "error",
        ms: e.latency_ms,
      };
      setDiary((d) => [line, ...d.filter((x) => x.id !== e.id)].slice(0, DIARY_MAX));
    });
    return () => {
      r.destroy();
      river.current = null;
    };
  }, []);

  useEffect(() => {
    if (map.data) river.current?.setMap(map.data);
  }, [map.data]);

  useEffect(() => {
    if (!summary.data) return;
    river.current?.setWindow(win);
    river.current?.setSummary(summary.data);
  }, [summary.data, win]);

  useEffect(() => {
    if (now) river.current?.push(events, now);
  }, [events, now]);

  useEffect(() => river.current?.setPaused(paused), [paused]);
  useEffect(() => river.current?.setSpeed(speed), [speed]);
  useEffect(() => {
    river.current?.setFollow(follow);
    if (!follow) setFollowed(null);
  }, [follow]);

  const head = summary.data?.head;
  const links = summary.data?.links ?? {};
  const totalCalls = Object.values(links).reduce((a, l) => a + l.calls, 0);
  const quiet = summary.data !== undefined && totalCalls === 0;
  const latency = head?.mcp_latency_ms != null ? `${num(head.mcp_latency_ms, 0)} ms` : "—";
  const label = (k: string) => nodes.get(k)?.label ?? k;

  const ariaLabel = head
    ? `Fiume dei flussi, ultimi ${win} min: ${num(head.calls_per_min)} chiamate al minuto, ${head.orders_today} ordini oggi, ` +
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
          <Stat value={head ? String(head.orders_today) : "—"} label="ordini oggi" />
          <Stat value={head ? String(head.errors_window) : "—"} label={`errori in ${win} min`} />
          <Stat value={latency} label="tempo medio" />
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11.5px] text-[#8b99c3]">
          <div className="flex gap-1">
            <Toggle on={paused} onClick={() => setPaused((p) => !p)}>
              {paused ? "▶ Riprendi" : "⏸ Pausa"}
            </Toggle>
            <Toggle on={follow} onClick={() => setFollow((f) => !f)}>
              ◎ Segui un ordine
            </Toggle>
          </div>
          <div role="group" aria-label="Velocità" className="flex items-center gap-1">
            velocità
            {SPEEDS.map((s) => (
              <Toggle key={s} on={speed === s} onClick={() => setSpeed(s)} label={`velocità ${s === 0.5 ? "metà" : `${s}×`}`}>
                {s === 0.5 ? "½×" : `${s}×`}
              </Toggle>
            ))}
          </div>
          <div role="group" aria-label="Larghezza su" className="flex items-center gap-1">
            larghezza su
            {WINDOWS.map((w) => (
              <Toggle key={w} on={win === w} onClick={() => setWin(w)}>
                {w} min
              </Toggle>
            ))}
          </div>
        </div>
      </div>

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
              Nessuna chiamata negli ultimi {win} minuti
            </div>
          )}
        </div>
        {follow && (
          <div className={cn(GLASS, "pointer-events-none absolute bottom-3 left-1/2 w-max max-w-[min(760px,92%)] -translate-x-1/2 px-4 py-2.5 text-center text-[13.5px]")}>
            <Story event={followed} label={label} />
          </div>
        )}
        {hover && <HoverCard hover={hover} host={wrapRef.current} nodes={nodes} label={label} win={win} summary={summary.data} river={river.current} lastPass={lastPass.current} mapLinks={map.data?.links ?? []} />}
      </div>

      {/* 2-minute timeline and live diary */}
      <div className={cn(GLASS, "flex h-[150px] shrink-0 gap-4 px-4 py-3")}>
        <div className="flex min-w-0 flex-1 flex-col">
          <h4 className={H4}>Ultimi 2 minuti · passaggi al secondo per chi li fa partire</h4>
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
            Chiamate in diretta
          </h4>
          <div role="log" aria-labelledby="flow-diary" className="h-[112px] overflow-hidden font-mono text-xs leading-[1.62] text-[#8b99c3]">
            {diary.length === 0 && <div className="italic">in attesa del primo passaggio…</div>}
            {diary.map((d) => (
              <div key={d.id} className="animate-in fade-in slide-in-from-right-2 truncate duration-300">
                {d.time}{" "}
                <b className="font-semibold" style={{ color: d.color }}>
                  {d.who}
                </b>{" "}
                <span className="text-[#cdb8ff]">{d.tool}</span>{" "}
                {d.error ? <span className="text-[#ff6f8c]">✕ errore</span> : d.ms != null ? `· ${d.ms} ms` : null}
              </div>
            ))}
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
  win: FlowWindow;
  summary: ReturnType<typeof useFlowSummary>["data"];
  river: RiverCanvas | null;
  lastPass: Map<string, { time: string; tool: string }>;
  mapLinks: [string, string][];
}

function HoverCard({ hover, host, nodes, label, win, summary, river, lastPass, mapLinks }: HoverProps) {
  const w = host?.clientWidth ?? 1200;
  const h = host?.clientHeight ?? 800;
  const style = { left: Math.max(0, Math.min(w - 350, hover.x + 18)), top: Math.max(0, Math.min(h - 190, hover.y + 14)) };
  let body: ReactNode = null;
  if (hover.node) {
    const n = nodes.get(hover.node);
    if (!n) return null;
    const pass = lastPass.get(n.key);
    const linked = mapLinks.filter(([a, b]) => a === n.key || b === n.key).length;
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
        </dl>
      </>
    );
  } else if (hover.link) {
    const [a, b] = hover.link.split("|");
    const s = summary?.links[hover.link];
    const dir = river?.linkDirection(hover.link);
    const tools = s?.top_tools.map((t) => `${t.tool} (${t.calls})`).join(", ");
    body = (
      <>
        <div className="mb-1 text-sm font-semibold">
          {label(a)} ↔ {label(b)}
        </div>
        <div className="mb-1.5 text-[#a9b5dc]">
          {dir ? `verso prevalente: ${label(dir[0])} → ${label(dir[1])}` : `nessun passaggio negli ultimi ${win} min`}
        </div>
        <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-xs text-[#8b99c3]">
          <dt>chiamate / min</dt>
          <dd className="num font-semibold text-[#e8eeff]">{num((s?.calls ?? 0) / win)}</dd>
          <dt>più frequenti</dt>
          <dd className="max-w-[180px] text-right font-semibold text-[#e8eeff]">{tools || "—"}</dd>
          <dt>errori in {win} min</dt>
          <dd className="num font-semibold text-[#e8eeff]">{s?.errors ?? 0}</dd>
        </dl>
      </>
    );
  }
  return (
    <div className={cn(GLASS, "pointer-events-none absolute z-10 min-w-[220px] max-w-[320px] px-3 py-2.5")} style={style}>
      {body}
    </div>
  );
}
