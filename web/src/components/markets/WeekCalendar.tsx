import { ExternalLink } from "lucide-react";
import { useWeekCalendar } from "@/api/hooks/marketsV2";
import type { CalendarEvent } from "@/api/marketsV2Types";
import { Badge } from "@/components/ui/badge";
import { byDay, dayLabel } from "@/lib/marketsV2";
import { cn } from "@/lib/utils";

const KIND: Record<CalendarEvent["kind"], string> = { earnings: "Trimestrale", ex_dividend: "Stacco dividendo", macro: "Banca centrale" };

function EventRow({ e, onOpen }: { e: CalendarEvent; onOpen: (s: string) => void }) {
  return (
    <li className={cn("flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded px-2 py-1 text-sm", e.held && "bg-accent/10")}>
      <Badge variant={e.kind === "macro" ? "default" : "secondary"} className="shrink-0">
        {KIND[e.kind]}
      </Badge>
      {e.symbol ? (
        <button type="button" className="num font-medium hover:underline" onClick={() => onOpen(e.symbol!)}>
          {e.symbol}
        </button>
      ) : (
        <span className="font-medium">{e.name}</span>
      )}
      <span className="min-w-0 flex-1 truncate text-muted">{e.symbol ? e.name : e.label}</span>
      {e.estimate && <span className="text-xs text-muted">data stimata</span>}
      {e.held && <span className="text-xs font-medium text-accent">in portafoglio: {e.portfolios.join(", ")}</span>}
    </li>
  );
}

/**
 * «Calendario della settimana»: this week's and next week's earnings and ex-dividend dates
 * of the catalog's equities (those held in a portfolio highlighted and first), plus the
 * central banks' meetings from their official 2026 calendars.
 */
export function WeekCalendar({ onOpen, onlyHeld = false }: { onOpen: (symbol: string) => void; onlyHeld?: boolean }) {
  const q = useWeekCalendar();
  const d = q.data;
  if (q.isLoading) return <p className="text-sm text-muted">Caricamento del calendario…</p>;
  if (q.isError || !d) return <p className="text-sm text-loss">Calendario non disponibile</p>;
  const events = onlyHeld ? d.events.filter((e) => e.held || e.kind === "macro") : d.events;
  const days = byDay(events);
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted">
        Dal {dayLabel(d.from)} al {dayLabel(d.to)} · date da Yahoo Finance per {d.covered} di {d.total} titoli
      </p>
      {days.length === 0 ? (
        <p className="text-sm text-muted">Nessun evento nelle due settimane</p>
      ) : (
        <div className="space-y-3">
          {days.map(([day, es]) => (
            <section key={day} aria-label={dayLabel(day)}>
              <h3 className={cn("mb-1 text-xs font-semibold uppercase tracking-wide", day === d.today ? "text-accent" : "text-muted")}>
                {dayLabel(day)}
                {day === d.today ? " · oggi" : ""}
              </h3>
              <ul className="space-y-0.5">
                {es.map((e) => (
                  <EventRow key={`${e.kind}:${e.symbol ?? e.name}:${e.date}`} e={e} onOpen={onOpen} />
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
      <p className="text-xs text-muted">
        {d.macro.note} Elenco aggiornato al {d.macro.as_of.split("-").reverse().join("/")}:{" "}
        {Object.entries(d.macro.sources).map(([bank, url], i) => (
          <span key={bank}>
            {i > 0 && " · "}
            <a href={url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-0.5 hover:underline">
              {bank}
              <ExternalLink className="h-3 w-3" aria-hidden />
            </a>
          </span>
        ))}
      </p>
    </div>
  );
}
