import { Pencil } from "lucide-react";
import { useState } from "react";
import type { RiskProfile } from "@/api/types";
import { Button } from "@/components/ui/button";
import { formatQty } from "@/lib/format";
import { assetClassLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { AdminOnly } from "@/components/AdminOnly";

function Chip({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "accent" | "gain" | "loss" }) {
  const tones = {
    neutral: "border-border bg-card text-fg",
    accent: "border-accent/40 bg-accent/10 text-fg",
    gain: "border-gain/40 bg-gain/10 text-gain",
    loss: "border-loss/40 bg-loss/10 text-loss",
  };
  return <span className={cn("inline-flex items-center rounded-md border px-2 py-0.5 text-xs", tones[tone])}>{children}</span>;
}

const limit = (v: string | null, suffix: string) => (v === null ? "nessun limite" : `${formatQty(v)}${suffix}`);

function dailyLoss(p: RiskProfile): string {
  const parts = [p.max_daily_loss_pct && `${formatQty(p.max_daily_loss_pct)}%`, p.max_daily_loss_eur && `${formatQty(p.max_daily_loss_eur)} €`].filter(Boolean);
  return parts.length ? parts.join(" / ") : "nessun limite";
}

/** One risk profile: limits as chips, description on two lines, whitelists summarised. */
export function RiskProfileCard({ profile: p, onEdit }: { profile: RiskProfile; onEdit: () => void }) {
  const [fullText, setFullText] = useState(false);
  const [showSymbols, setShowSymbols] = useState(false);
  const usedBy = p.portfolios_count ?? 0;
  return (
    <li aria-label={`Profilo ${p.name}`} className="min-w-0 rounded-lg border border-border p-3 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="break-words font-medium">{p.name}</h3>
          <p className="text-xs text-muted">
            {usedBy === 0 ? "Nessun portafoglio attivo" : usedBy === 1 ? "1 portafoglio attivo" : `${usedBy} portafogli attivi`}
          </p>
        </div>
        <AdminOnly>
          <Button size="sm" variant="outline" aria-label={`Modifica ${p.name}`} onClick={onEdit}>
            <Pencil />
            Modifica
          </Button>
        </AdminOnly>
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        <Chip>Ordine max: {limit(p.max_order_notional_eur, " €")}</Chip>
        <Chip>Posizione max: {limit(p.max_position_pct, "%")}</Chip>
        <Chip>Perdita giorno: {dailyLoss(p)}</Chip>
        <Chip tone={p.short_allowed ? "accent" : "neutral"}>Scoperto: {p.short_allowed ? "sì" : "no"}</Chip>
      </div>

      {p.description && (
        <div className="mt-2 text-sm">
          <p title={p.description} className={cn("break-words text-muted", !fullText && "line-clamp-2")}>
            {p.description}
          </p>
          {p.description.length > 120 && (
            <button type="button" className="text-xs text-accent hover:underline" onClick={() => setFullText((v) => !v)}>
              {fullText ? "Mostra meno" : "Mostra tutto"}
            </button>
          )}
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
        <Chip>Classi: {p.allowed_asset_classes.length ? p.allowed_asset_classes.map(assetClassLabel).join(", ") : "tutte"}</Chip>
        <Chip>Mercati: {p.allowed_markets.length ? p.allowed_markets.join(", ") : "tutti"}</Chip>
        {p.allowed_symbols.length === 0 ? (
          <Chip>Simboli: tutti</Chip>
        ) : (
          <button
            type="button"
            aria-expanded={showSymbols}
            className="inline-flex items-center rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-xs hover:bg-accent/20"
            onClick={() => setShowSymbols((v) => !v)}
          >
            Simboli: {p.allowed_symbols.length}
          </button>
        )}
      </div>
      {showSymbols && (
        <ul aria-label={`Simboli ammessi da ${p.name}`} className="mt-2 flex max-h-32 flex-wrap gap-1 overflow-y-auto">
          {p.allowed_symbols.map((s) => (
            <li key={s} className="rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-xs">
              {s}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
