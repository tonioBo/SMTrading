import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { useMarketSnapshot } from "@/api/hooks/marketsExplore";
import type { Mover, SectorTile } from "@/api/marketsTypes";
import { Pct } from "@/components/Pct";
import { selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { formatClock, formatMoney } from "@/lib/format";
import { tileStyle } from "@/components/markets/labels";

function MoverList({ title, rows, onOpen }: { title: string; rows: Mover[]; onOpen: (s: string) => void }) {
  return (
    <div className="min-w-0">
      <h3 className="mb-2 text-sm font-medium">{title}</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-muted">Nessuno</p>
      ) : (
        <ol className="space-y-1" aria-label={title}>
          {rows.map((m) => (
            <li key={m.symbol}>
              <button type="button" className="flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-sm hover:bg-card" onClick={() => onOpen(m.symbol)}>
                <span className="num w-20 shrink-0 font-medium">{m.symbol}</span>
                <span className="min-w-0 flex-1 truncate text-muted">{m.name}</span>
                <span className="num hidden shrink-0 text-xs text-muted sm:inline">{formatMoney(m.last, m.currency)}</span>
                <Pct value={m.change_pct} tone className="w-16 shrink-0 text-right" />
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function SectorDetail({ tile, onBack, onOpen }: { tile: SectorTile; onBack: () => void; onOpen: (s: string) => void }) {
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onBack}>
          <ArrowLeft className="mr-1 h-4 w-4" />
          Tutti i settori
        </Button>
        <span className="font-medium">{tile.sector}</span>
        <Pct value={tile.avg_change_pct} tone />
        <span className="text-xs text-muted">
          {tile.count} titoli · {tile.up} in rialzo · {tile.down} in ribasso
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5" aria-label={`Titoli del settore ${tile.sector}`}>
        {tile.members.map((m) => (
          <button key={m.symbol} type="button" style={tileStyle(m.change_pct)} className="rounded-md p-2 text-left text-sm" onClick={() => onOpen(m.symbol)} title={m.name}>
            <span className="num block font-medium">{m.symbol}</span>
            <span className="block truncate text-xs">{m.name}</span>
            <Pct value={m.change_pct} className="text-xs" />
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * «Mappa dei settori» and «Titoli più mossi» of a universe: one snapshot request (day
 * changes from batch calls, cached 5 minutes on the server) while the panel is shown.
 */
export function MarketOverview({
  universe,
  universes,
  onUniverse,
  onOpen,
}: {
  universe: string;
  universes: { code: string; label: string }[];
  onUniverse: (u: string) => void;
  onOpen: (symbol: string) => void;
}) {
  const snap = useMarketSnapshot(universe);
  const [sector, setSector] = useState<string | null>(null);
  const tile = snap.data?.sectors.find((t) => t.sector === sector) ?? null;
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <select
          aria-label="Universo"
          className={`${selectClass} sm:w-64`}
          value={universe}
          onChange={(e) => {
            setSector(null);
            onUniverse(e.target.value);
          }}
        >
          {universes.map((u) => (
            <option key={u.code} value={u.code}>
              {u.label}
            </option>
          ))}
        </select>
        {snap.data && (
          <span className="text-xs text-muted">
            Variazione dalla chiusura precedente, media semplice per settore · {snap.data.covered} di {snap.data.total} titoli · agg. {formatClock(snap.data.as_of)}
          </span>
        )}
      </div>
      {snap.isError && <p className="text-loss">Impossibile caricare la mappa dei settori</p>}
      {snap.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {snap.data && snap.data.total === 0 && <p className="text-sm text-muted">Nessuno strumento in questo universo</p>}
      {snap.data && snap.data.total > 0 && snap.data.covered === 0 && <p className="text-sm text-muted">Variazioni non disponibili al momento</p>}
      {snap.data &&
        (tile ? (
          <SectorDetail tile={tile} onBack={() => setSector(null)} onOpen={onOpen} />
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4" aria-label="Mappa dei settori">
            {snap.data.sectors.map((t) => (
              <button
                key={t.sector}
                type="button"
                style={tileStyle(t.avg_change_pct)}
                className="flex min-h-20 flex-col justify-between rounded-md p-3 text-left"
                onClick={() => setSector(t.sector)}
                aria-label={`${t.sector}: media ${t.avg_change_pct}%, ${t.count} titoli`}
              >
                <span className="text-sm font-medium leading-tight">{t.sector}</span>
                <span className="mt-1 flex items-baseline justify-between gap-2">
                  <Pct value={t.avg_change_pct} className="text-base font-semibold" />
                  <span className="text-xs">
                    {t.count} · ▲{t.up} ▼{t.down}
                  </span>
                </span>
              </button>
            ))}
          </div>
        ))}
      {snap.data && snap.data.covered > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          <MoverList title="Migliori 10" rows={snap.data.gainers} onOpen={onOpen} />
          <MoverList title="Peggiori 10" rows={snap.data.losers} onOpen={onOpen} />
        </div>
      )}
    </div>
  );
}
