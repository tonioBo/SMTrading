/** The TV screens «Campagna e portafogli» and «Salute e avvisi»: large type, high
 * contrast, no interaction (readable from 3–4 m on a 4K TV). Sizes are in `em` of the TV
 * root, which scales with the screen width. */
import type { ReactNode } from "react";
import type { TvCampaignData, TvHealthData } from "@/api/tvTypes";
import { formatClock, formatDecimal, formatMoney, formatPct, formatQty, formatSignedMoney, signTone } from "@/lib/format";
import { cn } from "@/lib/utils";
import { usePoll } from "@/tv/usePoll";

export const CAMPAIGN_MS = 15_000;
export const HEALTH_MS = 10_000;

export const TV_COLORS = {
  ok: "#3ee6a0",
  warn: "#ffc24d",
  bad: "#ff5c7a",
  off: "#8a96b8",
  muted: "#a9b5dc",
};

const tone = (v: string | null | undefined) => {
  const t = signTone(v);
  return t === "gain" ? TV_COLORS.ok : t === "loss" ? TV_COLORS.bad : undefined;
};

export function Panel({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn("flex min-h-0 flex-col rounded-[0.8em] border border-white/15 bg-white/[.05] p-[0.9em]", className)}>
      <h2 className="mb-[0.5em] text-[1.05em] font-semibold uppercase tracking-[.12em] text-[#a9b5dc]">{title}</h2>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-[1.2em] italic text-[#a9b5dc]">{children}</p>;
}

export function Waiting({ failed }: { failed: boolean }) {
  return (
    <div role="status" className="flex flex-1 items-center justify-center text-[2em] text-[#a9b5dc]">
      {failed ? "Dati non disponibili, nuovo tentativo…" : "Caricamento…"}
    </div>
  );
}

const STOP_TYPE: Record<string, string> = { stop: "stop", stop_limit: "stop limite", trailing_stop: "stop mobile" };
const CAMPAIGN_STATE: Record<string, string> = { prima: "non ancora iniziata", in_corso: "in corso", conclusa: "conclusa" };

export function CampaignScreen() {
  const { data, failed } = usePoll<TvCampaignData>(() => "/api/tv-display/campaign", CAMPAIGN_MS);
  if (!data) return <Waiting failed={failed} />;
  const { campaign, equity, goal } = data;
  const pct = goal ? Math.max(0, Math.min(100, (Number(goal.pct) / Math.max(Number(goal.target_pct), 0.0001)) * 100)) : 0;
  return (
    <div className="grid min-h-0 flex-1 grid-cols-[1.1fr_1fr] grid-rows-[auto_1fr] gap-[1em]">
      <Panel title="Campagna">
        {campaign ? (
          <>
            <div className="text-[1.7em] font-semibold leading-tight">{campaign.name}</div>
            <div className="mt-[0.2em] text-[1.4em] text-[#cfd8f5]">
              {campaign.state === "in_corso" ? `Giorno ${campaign.day} di ${campaign.total_days}` : CAMPAIGN_STATE[campaign.state]}
            </div>
          </>
        ) : (
          <Empty>Nessuna campagna in corso</Empty>
        )}
        <div className="mt-[0.6em] text-[1.1em] text-[#a9b5dc]">Valore totale{equity ? ` · ${equity.portfolios} portafogli` : ""}</div>
        <div className="num text-[3.6em] font-semibold leading-none">{equity ? formatMoney(equity.current_eur, "EUR") : "—"}</div>
        {goal && (
          <div className="mt-[0.8em]" aria-label="Andamento verso l'obiettivo">
            <div className="flex justify-between text-[1.2em]">
              <span>{goal.label || goal.name}</span>
              <span className="num" style={{ color: tone(goal.pct) }}>
                {formatPct(goal.pct)} su {formatPct(goal.target_pct)}
              </span>
            </div>
            <div className="mt-[0.3em] h-[0.9em] overflow-hidden rounded-full bg-white/10">
              <div className="h-full rounded-full" style={{ width: `${pct}%`, background: TV_COLORS.ok }} />
            </div>
            <div className="num mt-[0.3em] text-[1em] text-[#a9b5dc]">
              da {formatMoney(goal.start_eur, "EUR")} a {formatMoney(goal.target_eur, "EUR")}
            </div>
          </div>
        )}
      </Panel>
      <Panel title="Portafogli oggi">
        {data.portfolios.length === 0 ? (
          <Empty>Nessun portafoglio</Empty>
        ) : (
          <ul className="flex flex-col gap-[0.35em]">
            {data.portfolios.map((p) => (
              <li key={p.id} className="flex items-baseline justify-between gap-[1em] text-[1.35em]">
                <span className="truncate">{p.name}</span>
                <span className="num whitespace-nowrap">
                  {p.equity_eur ? formatMoney(p.equity_eur, "EUR") : "—"}{" "}
                  <b style={{ color: tone(p.day_change_eur) }}>
                    {p.day_change_eur ? formatSignedMoney(p.day_change_eur, "EUR") : ""}
                    {p.day_change_pct ? ` (${formatPct(p.day_change_pct)})` : ""}
                  </b>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title="Posizioni aperte">
        {data.positions.length === 0 ? (
          <Empty>Nessuna posizione aperta</Empty>
        ) : (
          <table className="w-full text-left text-[1.2em]">
            <thead className="text-[0.8em] text-[#a9b5dc]">
              <tr>
                <th className="font-normal">Titolo</th>
                <th className="font-normal">Portafoglio</th>
                <th className="text-right font-normal">Quantità</th>
                <th className="text-right font-normal">Prezzo medio</th>
                <th className="text-right font-normal">Stop</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.slice(0, 12).map((p) => (
                <tr key={`${p.portfolio}|${p.symbol}`}>
                  <td className="font-semibold">{p.symbol}</td>
                  <td className="truncate text-[#cfd8f5]">{p.portfolio}</td>
                  <td className="num text-right">{formatQty(p.qty)}</td>
                  <td className="num text-right">{formatDecimal(p.avg_price, 2)}</td>
                  <td className="num text-right" style={{ color: p.stop_price || p.trail_pct ? undefined : TV_COLORS.warn }}>
                    {p.stop_type === "trailing_stop" && p.trail_pct
                      ? `mobile ${formatDecimal(p.trail_pct, 1)}%`
                      : p.stop_price
                        ? `${STOP_TYPE[p.stop_type ?? "stop"] ?? "stop"} ${formatDecimal(p.stop_price, 2)}`
                        : "senza stop"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data.positions.length > 12 && <div className="mt-[0.3em] text-[1em] text-[#a9b5dc]">e altre {data.positions.length - 12}</div>}
      </Panel>
      <Panel title="Ultimi ordini eseguiti">
        {data.fills.length === 0 ? (
          <Empty>Nessun ordine eseguito</Empty>
        ) : (
          <ul className="flex flex-col gap-[0.3em] text-[1.2em]">
            {data.fills.slice(0, 10).map((f, i) => (
              <li key={`${f.at}|${f.symbol}|${i}`} className="flex justify-between gap-[1em]">
                <span>
                  <span className="num text-[#a9b5dc]">{formatClock(f.at, new Date(data.now))}</span>{" "}
                  <b style={{ color: f.side === "buy" ? TV_COLORS.ok : TV_COLORS.bad }}>{f.side === "buy" ? "acquisto" : "vendita"}</b> {f.symbol}
                </span>
                <span className="num whitespace-nowrap">
                  {formatQty(f.qty)} × {formatDecimal(f.price, 2)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {data.notes.length > 0 && (
          <ul className="mt-auto pt-[0.5em] text-[0.95em] text-[#a9b5dc]">
            {data.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

const STATUS_TEXT: Record<string, string> = { ok: "Tutto ok", attenzione: "Attenzione", problema: "Problema", spento: "Spento" };
const STATUS_COLOR: Record<string, string> = { ok: TV_COLORS.ok, attenzione: TV_COLORS.warn, problema: TV_COLORS.bad, spento: TV_COLORS.off };
const NODE_COLOR: Record<string, string> = { ok: TV_COLORS.ok, attenzione: TV_COLORS.warn, errore: TV_COLORS.bad, sconosciuto: TV_COLORS.off };

export function HealthScreen() {
  const { data, failed } = usePoll<TvHealthData>(() => "/api/tv-display/health", HEALTH_MS);
  if (!data) return <Waiting failed={failed} />;
  const now = new Date(data.now);
  const badNodes = data.nodes.filter((n) => n.state !== "ok");
  return (
    <div className="grid min-h-0 flex-1 grid-cols-[1.3fr_1fr] gap-[1em]">
      <div className="flex min-h-0 flex-col gap-[1em]">
        <div role="status" aria-label="Stato complessivo" className="flex items-center gap-[0.6em] text-[3em] font-semibold" style={{ color: STATUS_COLOR[data.status] }}>
          <span className="inline-block h-[0.7em] w-[0.7em] rounded-full" style={{ background: STATUS_COLOR[data.status] }} aria-hidden />
          {STATUS_TEXT[data.status] ?? data.status}
          <span className="num ml-auto text-[0.4em] font-normal text-[#a9b5dc]">controllo delle {formatClock(data.checked_at, now)}</span>
        </div>
        <div className="grid grid-cols-2 gap-[0.7em]">
          {data.sections.map((s) => (
            <div key={s.key} aria-label={s.label} className="rounded-[0.6em] border-l-[0.35em] bg-white/[.05] p-[0.6em]" style={{ borderColor: STATUS_COLOR[s.status] }}>
              <div className="flex justify-between text-[1.3em] font-semibold">
                {s.label}
                <span style={{ color: STATUS_COLOR[s.status] }}>{STATUS_TEXT[s.status] ?? s.status}</span>
              </div>
              <div className="mt-[0.2em] line-clamp-2 text-[1.05em] text-[#cfd8f5]">{s.summary}</div>
            </div>
          ))}
        </div>
        {data.nodes.length > 0 && (
          <Panel title="Flussi">
            {badNodes.length === 0 ? (
              <div className="text-[1.3em]" style={{ color: TV_COLORS.ok }}>
                Tutti i {data.nodes.length} punti del Fiume sono a posto
              </div>
            ) : (
              <ul className="flex flex-col gap-[0.3em] text-[1.2em]">
                {badNodes.slice(0, 8).map((n) => (
                  <li key={n.key}>
                    <b style={{ color: NODE_COLOR[n.state] }}>{n.label}</b> <span className="text-[#cfd8f5]">{n.reason}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        )}
      </div>
      <div className="flex min-h-0 flex-col gap-[1em]">
        <Panel title="Avvisi aperti">
          {!data.alerts.on && <Empty>Avvisi di sistema spenti</Empty>}
          {data.alerts.on && data.alerts.open.length === 0 && <div className="text-[1.3em]" style={{ color: TV_COLORS.ok }}>Nessun avviso aperto</div>}
          <ul className="flex flex-col gap-[0.35em] text-[1.25em]">
            {data.alerts.open.map((a) => (
              <li key={`${a.label}|${a.since}`}>
                <b style={{ color: a.level === "problema" || a.level === "errore" ? TV_COLORS.bad : TV_COLORS.warn }}>{a.title || a.label}</b>{" "}
                <span className="num text-[#a9b5dc]">dalle {formatClock(a.since, now)}</span>
              </li>
            ))}
          </ul>
        </Panel>
        <Panel title="Ultimi avvisi" className="flex-1">
          {data.alerts.recent.length === 0 ? (
            <Empty>Nessun avviso recente</Empty>
          ) : (
            <ul className="flex flex-col gap-[0.3em] text-[1.15em]">
              {data.alerts.recent.map((a) => (
                <li key={`${a.label}|${a.at}`}>
                  <span className="num text-[#a9b5dc]">{formatClock(a.at, now)}</span> {a.label}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  );
}
