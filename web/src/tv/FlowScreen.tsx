/** The TV «Fiume dei flussi»: the portal's river renderer full screen, with large
 * counters and diary, no controls. One read every 3 s brings map, widths, health and the
 * new events (`after=<cursor>`). */
import { useEffect, useRef, useState } from "react";
import type { FlowEvent, FlowNode } from "@/api/types";
import type { TvFlowData } from "@/api/tvTypes";
import { kindColor, RiverCanvas } from "@/components/flow/RiverCanvas";
import { clock, num } from "@/components/flow/styles";
import { Waiting } from "@/tv/screens";
import { usePoll } from "@/tv/usePoll";

export const FLOW_TV_MS = 3_000;
export const DIARY_TV_MAX = 6;
/** the summary the display route reads (GET /api/flow/summary?window=5) */
const WINDOW_MIN = 5;

interface Line {
  id: string;
  time: string;
  who: string;
  color: string;
  what: string;
  error: boolean;
}

/** a cheap identity of the map: setMap only when nodes or links change */
const mapSig = (d: TvFlowData) => `${d.map.nodes.map((n) => n.key).join(",")}|${d.map.links.map((l) => l.join(">")).join(",")}`;

function Counter({ value, label }: { value: string; label: string }) {
  return (
    <div className="text-[1.05em] text-[#a9b5dc]">
      <b className="num block text-[2.4em] font-semibold leading-none text-white">{value}</b>
      {label}
    </div>
  );
}

export function FlowScreen() {
  const riverRef = useRef<HTMLCanvasElement>(null);
  const timelineRef = useRef<HTMLCanvasElement>(null);
  const river = useRef<RiverCanvas | null>(null);
  const nodes = useRef(new Map<string, FlowNode>());
  const sig = useRef("");
  const [diary, setDiary] = useState<Line[]>([]);

  useEffect(() => {
    const r = new RiverCanvas(riverRef.current!, timelineRef.current!);
    river.current = r;
    r.onLaunch((e: FlowEvent) => {
      const n = nodes.current.get(e.who);
      const line: Line = {
        id: e.id,
        time: clock(e.ts),
        who: n?.label ?? e.who,
        color: kindColor(n?.kind ?? "core"),
        what: e.kind === "order_sent" ? (e.hops[1] ?? e.hops[0] ?? e.kind) : (e.hops[0] ?? e.kind),
        error: e.outcome === "error",
      };
      setDiary((d) => [line, ...d.filter((x) => x.id !== e.id)].slice(0, DIARY_TV_MAX));
    });
    r.setWindow(WINDOW_MIN);
    return () => {
      r.destroy();
      river.current = null;
    };
  }, []);

  const { data, failed } = usePoll<TvFlowData>(
    (prev) => (prev ? `/api/tv-display/flow?after=${encodeURIComponent(prev.cursor)}` : "/api/tv-display/flow"),
    FLOW_TV_MS,
    (d) => {
      const r = river.current;
      if (!r) return;
      const s = mapSig(d);
      if (s !== sig.current) {
        sig.current = s;
        nodes.current = new Map(d.map.nodes.map((n) => [n.key, n]));
        r.setMap(d.map);
      }
      r.setSummary(d.summary);
      r.setHealth(d.health.nodes);
      r.push(d.events, d.now);
    },
  );

  const head = data?.summary.head;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-[0.8em]">
      <div role="group" aria-label="Contatori" className="flex gap-[3em]">
        <Counter value={head ? num(head.calls_per_min) : "—"} label="chiamate al minuto" />
        <Counter value={head ? String(head.orders_today) : "—"} label="ordini oggi" />
        <Counter value={head ? String(head.errors_window) : "—"} label={`errori in ${WINDOW_MIN} min`} />
        <Counter value={head?.mcp_latency_ms != null ? `${num(head.mcp_latency_ms, 0)} ms` : "—"} label="tempo medio" />
      </div>
      <div className="relative min-h-0 flex-1">
        <canvas ref={riverRef} role="img" aria-label="Fiume dei flussi" className="absolute left-0 top-0" />
        {!data && (
          <div className="absolute inset-0 flex">
            <Waiting failed={failed} />
          </div>
        )}
      </div>
      <div className="flex h-[9em] shrink-0 gap-[1.5em]">
        <div className="relative min-w-0 flex-1">
          <canvas ref={timelineRef} aria-hidden className="absolute left-0 top-0" />
        </div>
        <div role="log" aria-label="Chiamate in diretta" className="w-[44%] overflow-hidden font-mono text-[1.05em] leading-[1.45] text-[#cfd8f5]">
          {diary.length === 0 && <div className="italic text-[#a9b5dc]">in attesa del primo passaggio…</div>}
          {diary.map((d) => (
            <div key={d.id} className="truncate">
              {d.time}{" "}
              <b style={{ color: d.color }}>{d.who}</b> <span className="text-[#d9c9ff]">{d.what}</span>
              {d.error && <span className="text-[#ff6f8c]"> ✕ errore</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
