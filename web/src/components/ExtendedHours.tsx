import type { ExtendedHours } from "@/api/types";
import { Money } from "@/components/Money";
import { Pct } from "@/components/Pct";
import { StatusBadge } from "@/components/StatusBadge";
import { formatClock } from "@/lib/format";
import { cn } from "@/lib/utils";

const INFO = "Orari estesi: solo informativo. Ordini, stop e valori usano solo la seduta regolare.";

/** «Mercati» quote card: «🌙 After-hours 255,40 $ (-0,8%) · 22:45» / «🌅 Pre-market …». */
export function ExtendedQuoteLine({ ext, currency = "USD", now }: { ext: ExtendedHours | null | undefined; currency?: string; now?: Date }) {
  if (!ext) return null;
  const label = ext.session === "pre" ? "🌅 Pre-market" : "🌙 After-hours";
  return (
    <div className="mt-1 text-xs text-muted" title={INFO} aria-label="Prezzo fuori orario">
      {label} <Money value={ext.price} currency={currency} /> (<Pct value={ext.change_pct_vs_close} places={1} />) · {formatClock(ext.ts, now)}
    </div>
  );
}

/** «Posizioni»: «fuori orario 255,40 $ (-0,8%)» under the last price. */
export function ExtendedPositionLine({ ext, currency, className }: { ext: ExtendedHours | null | undefined; currency: string; className?: string }) {
  if (!ext) return null;
  return (
    <div className={cn("text-xs text-muted", className)} title={`${ext.session === "pre" ? "Pre-market" : "After-hours"} · ${INFO}`}>
      fuori orario <Money value={ext.price} currency={currency} /> (<Pct value={ext.change_pct_vs_close} places={1} />)
    </div>
  );
}

/** Amber badge: the extended-hours price is already beyond the stop (gap risk at the open). */
export function GapRiskBadge({ side }: { side: "long" | "short" }) {
  return (
    <StatusBadge tone="warn" title={INFO}>
      {`⚠ ${side === "short" ? "sopra" : "sotto"} lo stop fuori orario: rischio gap all'apertura`}
    </StatusBadge>
  );
}
