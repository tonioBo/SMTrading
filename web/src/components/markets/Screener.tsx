import { useState } from "react";
import { useScreener } from "@/api/hooks/marketsV2";
import type { ScreenerPreset, ScreenerRow } from "@/api/marketsV2Types";
import { DataTable, type Column } from "@/components/DataTable";
import { selectClass } from "@/components/FormField";
import { FavoriteStar } from "@/components/markets/FavoriteStar";
import { Pct } from "@/components/Pct";
import { Button } from "@/components/ui/button";
import { formatDateTime, formatDecimal, formatMoney } from "@/lib/format";

const PRESET_LABELS: Record<ScreenerPreset, string> = {
  rsi_oversold: "RSI < 30",
  rsi_overbought: "RSI > 70",
  above_sma200: "Sopra SMA 200",
  below_sma200: "Sotto SMA 200",
  new_high: "Nuovi massimi 52s",
  new_low: "Nuovi minimi 52s",
  volume_spike: "Volume > 2× media",
  near_low: "Vicino ai minimi",
};

const n = (v: string | null) => (v == null ? -Infinity : Number(v));

/**
 * «Screener tecnico»: preset filters over the catalog (or a universe) on figures the
 * worker computes from daily bars after each close — the page reads them only, so it is
 * instant. A row opens the instrument's sheet.
 */
export function Screener({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [preset, setPreset] = useState<ScreenerPreset>("rsi_oversold");
  const [universe, setUniverse] = useState("ALL");
  const q = useScreener(preset, universe);
  const d = q.data;
  const columns: Column<ScreenerRow>[] = [
    {
      key: "symbol",
      header: "Simbolo",
      primary: true,
      sortValue: (r) => r.symbol,
      cell: (r) => (
        <span className="flex items-center gap-1">
          <FavoriteStar symbol={r.symbol} />
          <button type="button" className="num font-medium hover:underline" onClick={() => onOpen(r.symbol)} aria-label={`Scheda ${r.symbol}`}>
            {r.symbol}
          </button>
        </span>
      ),
    },
    { key: "name", header: "Nome", hideOnMobile: true, sortValue: (r) => r.name, cell: (r) => <span className="block max-w-[14rem] truncate">{r.name}</span> },
    { key: "close", header: "Chiusura", align: "right", sortValue: (r) => n(r.close), cell: (r) => <span className="num">{formatMoney(r.close, r.currency)}</span> },
    { key: "chg", header: "Var. 1g", align: "right", sortValue: (r) => n(r.chg_1d), cell: (r) => <Pct value={r.chg_1d} tone /> },
    { key: "rsi", header: "RSI 14", align: "right", sortValue: (r) => n(r.rsi14), cell: (r) => <span className="num">{r.rsi14 ? formatDecimal(r.rsi14, 1) : "—"}</span> },
    { key: "sma", header: "vs SMA 200", align: "right", sortValue: (r) => n(r.vs_sma200_pct), cell: (r) => <Pct value={r.vs_sma200_pct} tone /> },
    { key: "low", header: "Dal min. 52s", align: "right", hideOnMobile: true, sortValue: (r) => n(r.from_low_pct), cell: (r) => <Pct value={r.from_low_pct} /> },
    { key: "high", header: "Dal max. 52s", align: "right", hideOnMobile: true, sortValue: (r) => n(r.from_high_pct), cell: (r) => <Pct value={r.from_high_pct} /> },
    { key: "vol", header: "Volume / media 20g", align: "right", sortValue: (r) => n(r.volume_ratio), cell: (r) => <span className="num">{r.volume_ratio ? `${formatDecimal(r.volume_ratio, 1)}×` : "—"}</span> },
  ];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1" role="group" aria-label="Filtri dello screener">
        {(Object.keys(PRESET_LABELS) as ScreenerPreset[]).map((p) => (
          <Button key={p} type="button" size="sm" variant={p === preset ? "default" : "outline"} aria-pressed={p === preset} onClick={() => setPreset(p)}>
            {PRESET_LABELS[p]}
          </Button>
        ))}
      </div>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <select aria-label="Universo dello screener" className={`${selectClass} sm:w-64`} value={universe} onChange={(e) => setUniverse(e.target.value)}>
          {(d?.universes ?? [{ code: "ALL", label: "Tutto il catalogo" }]).map((u) => (
            <option key={u.code} value={u.code}>
              {u.label}
            </option>
          ))}
        </select>
        {d && (
          <span className="text-xs text-muted">
            {d.label}: {d.matched} titoli · calcolati {d.computed} di {d.total}
            {d.as_of ? ` · aggiornato ${formatDateTime(d.as_of).slice(0, 16)}` : ""}
          </span>
        )}
      </div>
      {q.isError && <p className="text-sm text-loss">Impossibile caricare lo screener</p>}
      {d && d.computed === 0 && <p className="text-sm text-muted">Dati in preparazione: lo screener si aggiorna dopo la chiusura dei mercati.</p>}
      <DataTable aria-label="Risultati dello screener" columns={columns} rows={d?.items} rowKey={(r) => r.symbol} loading={q.isLoading} empty="Nessun titolo soddisfa il filtro" />
      <p className="text-xs text-muted">Su barre giornaliere chiuse: RSI a 14 sedute, media mobile a 200 sedute, massimi e minimi degli ultimi 365 giorni.</p>
    </div>
  );
}
