import { ArrowLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useMarketSnapshot } from "@/api/hooks/marketsExplore";
import type { MarketSnapshot, Mover, SectorTile } from "@/api/marketsTypes";
import { SectorTreemap } from "@/components/charts";
import { Pct } from "@/components/Pct";
import { selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { formatClock, formatMoney } from "@/lib/format";
import { FULL_SCALE } from "@/lib/sectorMap";
import type { SnapshotPeriod } from "@/api/marketsV2Types";

const PERIODS: { code: SnapshotPeriod; label: string; long: string }[] = [
  { code: "1d", label: "1g", long: "di oggi" },
  { code: "1w", label: "1s", long: "a una settimana" },
  { code: "1m", label: "1m", long: "a un mese" },
  { code: "ytd", label: "Da inizio anno", long: "da inizio anno" },
];

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

/** The breadcrumb of the map: «Tutti i settori» › the opened sector and its counts. */
function Breadcrumb({ tile, onBack }: { tile: SectorTile | null; onBack: () => void }) {
  return (
    <nav aria-label="Percorso della mappa" className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
      {tile ? (
        <>
          <Button type="button" size="sm" variant="outline" onClick={onBack}>
            <ArrowLeft className="mr-1 h-4 w-4" />
            Tutti i settori
          </Button>
          <ChevronRight className="h-4 w-4 text-muted" aria-hidden />
          <span className="font-medium" aria-current="page">
            {tile.sector}
          </span>
          <Pct value={tile.avg_change_pct} tone />
          <span className="text-xs text-muted">
            {tile.count} titoli · {tile.up} in rialzo · {tile.down} in ribasso
          </span>
        </>
      ) : (
        <span className="font-medium" aria-current="page">
          Tutti i settori
        </span>
      )}
      <span className="text-xs text-muted sm:ml-auto">area: numero di titoli · colore: variazione media (±{FULL_SCALE}%)</span>
    </nav>
  );
}

/**
 * The map's tiles as buttons for keyboards and screen readers (the treemap is a canvas):
 * visually hidden, same order and actions.
 */
function TileList({ snapshot, tile, onSector, onOpen }: { snapshot: MarketSnapshot; tile: SectorTile | null; onSector: (s: string) => void; onOpen: (s: string) => void }) {
  if (tile) {
    return (
      <ul className="sr-only" aria-label={`Titoli del settore ${tile.sector}`}>
        {tile.members.map((m) => (
          <li key={m.symbol}>
            <button type="button" onClick={() => onOpen(m.symbol)}>{`${m.symbol} ${m.name}: ${m.change_pct}%`}</button>
          </li>
        ))}
      </ul>
    );
  }
  return (
    <ul className="sr-only" aria-label="Mappa dei settori">
      {snapshot.sectors.map((t) => (
        <li key={t.sector}>
          <button type="button" onClick={() => onSector(t.sector)}>{`${t.sector}: media ${t.avg_change_pct}%, ${t.count} titoli, ${t.up} in rialzo, ${t.down} in ribasso`}</button>
        </li>
      ))}
    </ul>
  );
}

/**
 * «Mappa dei settori» and «Titoli più mossi» of a universe: one snapshot request (day
 * changes from batch calls, cached 5 minutes on the server) while the panel is shown. The
 * map is an ECharts treemap (lazily loaded): sectors by member count coloured by their
 * average change; a click opens a sector's members, a member's click its sheet.
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
  const [period, setPeriod] = useState<SnapshotPeriod>("1d");
  const snap = useMarketSnapshot(universe, true, period);
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
        <div className="flex gap-1" role="group" aria-label="Periodo della mappa">
          {PERIODS.map((p) => (
            <Button
              key={p.code}
              type="button"
              size="sm"
              variant={p.code === period ? "default" : "outline"}
              aria-pressed={p.code === period}
              onClick={() => {
                setSector(null);
                setPeriod(p.code);
              }}
            >
              {p.label}
            </Button>
          ))}
        </div>
        {snap.data && (
          <span className="text-xs text-muted">
            {period === "1d" ? "Variazione dalla chiusura precedente" : `Variazione ${PERIODS.find((p) => p.code === period)?.long} sulle chiusure giornaliere`}, media semplice per settore · {snap.data.covered} di {snap.data.total} titoli
            {snap.data.as_of ? ` · agg. ${formatClock(snap.data.as_of)}` : ""}
          </span>
        )}
      </div>
      {snap.isError && <p className="text-loss">Impossibile caricare la mappa dei settori</p>}
      {snap.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {snap.data && snap.data.total === 0 && <p className="text-sm text-muted">Nessuno strumento in questo universo</p>}
      {snap.data && snap.data.total > 0 && snap.data.covered === 0 && (
        <p className="text-sm text-muted">{period === "1d" ? "Variazioni non disponibili al momento" : "Variazioni in preparazione: si aggiornano dopo la chiusura dei mercati"}</p>
      )}
      {snap.data && snap.data.sectors.length > 0 && (
        <div>
          <Breadcrumb tile={tile} onBack={() => setSector(null)} />
          <SectorTreemap snapshot={snap.data} sector={tile} onSector={setSector} onMember={onOpen} />
          <TileList snapshot={snap.data} tile={tile} onSector={setSector} onOpen={onOpen} />
        </div>
      )}
      {snap.data && snap.data.covered > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          <MoverList title="Migliori 10" rows={snap.data.gainers} onOpen={onOpen} />
          <MoverList title="Peggiori 10" rows={snap.data.losers} onOpen={onOpen} />
        </div>
      )}
    </div>
  );
}
