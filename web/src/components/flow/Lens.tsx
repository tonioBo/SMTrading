import { useEffect, useMemo, useRef, useState } from "react";
import type { FlowEvent, FlowPriceSource, FlowSources, FlowTools, FlowWindow } from "@/api/types";
import { cn } from "@/lib/utils";
import { windowLabel, type LensKind } from "./replay";
import { H4, num, PANEL } from "./styles";
import { useDialog } from "./useDialog";

/** «Lente sui metodi»: a node opened on request, over the river (never in the general
 * view). «MCP SMTrading» opens into its tool families (GET /api/flow/tools), each a small
 * current with its tools; «Prezzi» into the market-data sources (GET /api/flow/sources).
 * Real comets run in the lens too (the events the river launches); a click outside or
 * Escape closes it, and the river underneath never stops. */

export type { LensKind } from "./replay";

export interface LensProps {
  kind: LensKind;
  /** the node's label from the map */
  title: string;
  win: FlowWindow;
  tools?: FlowTools;
  sources?: FlowSources;
  /** during a replay: the counts are the live ones (the tools route has no past window) */
  live?: boolean;
  /** the events the river launches; returns the unsubscribe */
  subscribe: (cb: (e: FlowEvent) => void) => () => void;
  onClose: () => void;
}

const COMET_MS = 1_400;
const COMETS_PER_ROW = 8;
const STATE_DOT: Record<FlowPriceSource["state"], string> = {
  live: "#34e0c4",
  connecting: "#ffb347",
  refused: "#ff4d6d",
  on_demand: "#5fd3ff",
  unknown: "#8b99c3",
};
const ASSET_IT: Record<string, string> = { stocks: "azioni", crypto: "cripto", etf: "ETF", fx: "valute" };

const plural = (n: number, one: string, many: string) => `${num(n, 0)} ${n === 1 ? one : many}`;

/** comets per row key, each removed after its run; subscribed once (a refetch of the
 * tools only changes how rows are found, through the ref) */
function useComets(subscribe: LensProps["subscribe"], rowOf: (e: FlowEvent) => string | null) {
  const [comets, setComets] = useState<{ id: string; row: string }[]>([]);
  const rowRef = useRef(rowOf);
  rowRef.current = rowOf;
  useEffect(() => {
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const off = subscribe((e) => {
      const row = rowRef.current(e);
      if (!row) return;
      setComets((c) => (c.some((x) => x.id === e.id) || c.filter((x) => x.row === row).length >= COMETS_PER_ROW ? c : [...c, { id: e.id, row }]));
      const t = setTimeout(() => {
        timers.delete(t);
        setComets((c) => c.filter((x) => x.id !== e.id));
      }, COMET_MS);
      timers.add(t);
    });
    return () => {
      off();
      timers.forEach(clearTimeout);
      setComets([]); // no comet left without its timer
    };
  }, [subscribe]);
  return comets;
}

function Band({ row, share, comets, color }: { row: string; share: number; comets: { id: string; row: string }[]; color: string }) {
  return (
    <div className="relative h-2.5 flex-1 overflow-hidden rounded-full bg-white/[.05]">
      <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.max(2, share * 100)}%`, background: `linear-gradient(90deg, ${color}55, ${color}aa)` }} />
      {comets
        .filter((c) => c.row === row)
        .map((c) => (
          <span
            key={c.id}
            data-comet={row}
            aria-hidden
            className="absolute top-1/2 h-2 w-6 -translate-y-1/2 rounded-full"
            style={{ background: `radial-gradient(circle at 80% 50%, #fff, ${color} 45%, transparent 75%)`, animation: `flow-lens-comet ${COMET_MS}ms linear forwards` }}
          />
        ))}
    </div>
  );
}

function McpLens({ tools, comets, win }: { tools: FlowTools; comets: { id: string; row: string }[]; win: FlowWindow }) {
  const rows = [...tools.families, ...(tools.other.length ? [{ key: "other", label: "altro", tools: tools.other }] : [])];
  const totals = rows.map((f) => f.tools.reduce((a, t) => a + t.calls, 0));
  const max = Math.max(1, ...totals);
  return (
    <>
      <p className="m-0 mb-2 text-xs text-[#8b99c3]">
        Gli strumenti degli agenti per famiglia, {win === "today" ? "da mezzanotte" : `ultimi ${windowLabel(win)}`}: chiamate, tempo medio ed errori.
        {tools.truncated ? " Lettura parziale: i numeri sono un minimo." : ""}
      </p>
      <ul className="m-0 list-none space-y-2.5 p-0">
        {rows.map((f, i) => {
          const errors = f.tools.reduce((a, t) => a + t.errors, 0);
          const sorted = [...f.tools].sort((a, b) => b.calls - a.calls || a.tool.localeCompare(b.tool));
          return (
            <li key={f.key} aria-label={f.label}>
              <div className="flex items-center gap-3">
                <span className="w-[150px] shrink-0 text-[12.5px] font-semibold text-[#e8eeff]">{f.label}</span>
                <Band row={f.key} share={totals[i] / max} comets={comets} color="#34e0c4" />
                <span className="num w-[150px] shrink-0 text-right text-xs text-[#8b99c3]">
                  {plural(totals[i], "chiamata", "chiamate")}
                  {errors ? <span className="text-[#ff6f8c]"> · {plural(errors, "errore", "errori")}</span> : null}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap gap-1 pl-[162px]">
                {sorted.map((t) => (
                  <span
                    key={t.tool}
                    className={cn("rounded-md border border-white/10 px-1.5 py-0.5 font-mono text-[11px]", t.calls ? "text-[#cdb8ff]" : "text-[#5f6f9e]")}
                  >
                    {t.tool}
                    {t.calls ? ` ${num(t.calls, 0)}` : ""}
                    {t.avg_ms != null && t.calls ? ` · ${num(t.avg_ms, 0)} ms` : ""}
                    {t.errors ? <span className="text-[#ff6f8c]"> · {t.errors} ✕</span> : null}
                  </span>
                ))}
              </div>
            </li>
          );
        })}
      </ul>
    </>
  );
}

function PriceLens({ sources, comets }: { sources: FlowSources; comets: { id: string; row: string }[] }) {
  return (
    <>
      <div className="mb-2.5 flex items-center gap-3">
        <span className="w-[150px] shrink-0 text-[12.5px] font-semibold text-[#e8eeff]">letture dei prezzi</span>
        <Band row="px" share={1} comets={comets} color="#5fd3ff" />
      </div>
      {!sources.stream_status && <p className="m-0 mb-2 text-xs italic text-[#ffd08f]">stato dei flussi non pubblicato: gli stati in diretta non sono noti</p>}
      <ul className="m-0 list-none space-y-2 p-0">
        {sources.sources.map((s) => (
          <li key={s.key} aria-label={s.label} className="rounded-xl border border-white/10 bg-white/[.03] px-3 py-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
              <span className="text-[13px] font-semibold text-[#e8eeff]">{s.label}</span>
              <span className="text-xs text-[#8b99c3]">{s.mode === "stream" ? "flusso in tempo reale" : "lettura a richiesta"}</span>
              <span className="flex items-center gap-1.5 text-xs font-semibold" style={{ color: STATE_DOT[s.state] }}>
                <i className="inline-block h-2 w-2 rounded-full" style={{ background: STATE_DOT[s.state] }} />
                {s.state_it}
              </span>
              <span className="num flex-1 text-right text-xs text-[#8b99c3]">
                {[
                  s.symbols ? `segue ${plural(s.symbols, "titolo", "titoli")}` : null,
                  s.updates_per_min != null ? `${num(s.updates_per_min)} aggiornamenti/min` : null,
                  s.reads_minute != null ? `${num(s.reads_minute, 0)} letture/min` : null,
                  s.reads_hour != null ? `${num(s.reads_hour, 0)}/ora` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </div>
            {s.streams.length > 0 && (
              <div className="mt-0.5 text-[11.5px] text-[#8b99c3]">
                {s.streams.map((st) => `${ASSET_IT[st.asset_class] ?? st.asset_class}: ${plural(st.symbols, "titolo", "titoli")}`).join(" · ")}
              </div>
            )}
            {s.symbol_list.length > 0 && <div className="mt-0.5 truncate font-mono text-[11px] text-[#6f80b3]">{s.symbol_list.join(" ")}</div>}
            {s.note && <div className="mt-0.5 text-[11.5px] text-[#a9b5dc]">{s.note}</div>}
            {s.last_error && <div className="mt-0.5 text-[11.5px] text-[#ff9fb2]">ultimo errore: {s.last_error}</div>}
          </li>
        ))}
      </ul>
      <p className="m-0 mt-2.5 text-xs text-[#8b99c3]">
        {sources.polling.length ? `a lettura periodica: ${sources.polling.join(", ")}` : "nessun titolo a lettura periodica"}
        {sources.disabled.length ? ` · spente: ${sources.disabled.join(", ")}` : ""}
      </p>
    </>
  );
}

export function Lens({ kind, title, win, tools, sources, live, subscribe, onClose }: LensProps) {
  const dialog = useRef<HTMLDivElement>(null);
  useDialog(dialog, onClose);
  const familyOf = useMemo(() => {
    const m = new Map<string, string>();
    tools?.families.forEach((f) => f.tools.forEach((t) => m.set(t.tool, f.key)));
    return m;
  }, [tools]);
  const rowOf = useMemo(
    () =>
      kind === "mcp"
        ? (e: FlowEvent) => (e.kind === "mcp" ? (familyOf.get(e.hops[0] ?? "") ?? "other") : null)
        : (e: FlowEvent) => (e.path.includes("px") ? "px" : null),
    [kind, familyOf],
  );
  const comets = useComets(subscribe, rowOf);

  const ready = kind === "mcp" ? tools : sources;
  return (
    <div data-testid="lens-backdrop" className="absolute inset-0 z-30 flex items-center justify-center bg-[rgba(3,5,12,.35)] p-4" onClick={onClose}>
      <style>
        {"@keyframes flow-lens-comet{from{left:-24px}to{left:100%}}" +
          "@media (prefers-reduced-motion: reduce){[data-comet]{animation:none!important;left:auto;right:4px}}"}
      </style>
      <div
        ref={dialog}
        tabIndex={-1}
        role="dialog"
        aria-label={`Lente su ${title}`}
        className={cn(PANEL, "max-h-full outline-none w-[min(820px,100%)] overflow-y-auto px-5 py-4 text-[13px] text-[#e8eeff]")}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-baseline gap-3">
          <h3 className="m-0 text-[16px] font-semibold">{title}</h3>
          <span className={cn(H4, "mb-0")}>{kind === "mcp" ? "lente sugli strumenti" : "fonti dei prezzi"}</span>
          <span className="flex-1" />
          <span className="text-[11.5px] text-[#6f80b3]">clic fuori o Esc per chiudere</span>
        </div>
        {live && <p className="m-0 mb-2 text-xs italic text-[#ffd08f]">conteggi in diretta: la moviola non li ricalcola (le comete seguono la moviola)</p>}
        {!ready ? (
          <div className="italic text-[#8b99c3]">caricamento…</div>
        ) : kind === "mcp" ? (
          <McpLens tools={tools!} comets={comets} win={win} />
        ) : (
          <PriceLens sources={sources!} comets={comets} />
        )}
      </div>
    </div>
  );
}
