import { useComposition } from "@/api/hooks/portfolios";
import type { CompositionGroup, CompositionPosition } from "@/api/types";
import { donutSlices, type Slice } from "@/lib/composition";
import { DataTable, type Column } from "@/components/DataTable";
import { Money } from "@/components/Money";
import { Pnl } from "@/components/Pnl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatDecimal, formatQty } from "@/lib/format";
import { assetClassLabel } from "@/lib/labels";

/** Arc length only (geometry, not displayed): a short is drawn by its absolute value. */
function magnitude(value: string): number {
  const n = Math.abs(Number(value));
  return Number.isFinite(n) ? n : 0;
}

function Pct({ value }: { value: string | null }) {
  return <span className="num">{value == null ? "—" : `${formatDecimal(value, 1)}%`}</span>;
}

const RADIUS = 40;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

function Donut({ slices, center }: { slices: Slice[]; center: string | null }) {
  const total = slices.reduce((s, x) => s + magnitude(x.value_eur), 0);
  let offset = 0;
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
      <svg viewBox="0 0 100 100" className="h-40 w-40 shrink-0 -rotate-90" role="img" aria-label="Composizione per titolo">
        <circle cx="50" cy="50" r={RADIUS} fill="none" stroke="rgb(var(--border))" strokeWidth="14" />
        {total > 0 &&
          slices.map((s) => {
            const len = (magnitude(s.value_eur) / total) * CIRCUMFERENCE;
            const dash = `${len} ${CIRCUMFERENCE - len}`;
            const el = (
              <circle
                key={s.key}
                data-testid={`slice-${s.key}`}
                cx="50"
                cy="50"
                r={RADIUS}
                fill="none"
                stroke={s.color}
                strokeWidth="14"
                strokeDasharray={dash}
                strokeDashoffset={-offset}
              >
                <title>{`${s.label}: ${s.weight_pct == null ? "—" : formatDecimal(s.weight_pct, 1) + "%"}`}</title>
              </circle>
            );
            offset += len;
            return el;
          })}
        <text x="50" y="50" transform="rotate(90 50 50)" textAnchor="middle" dominantBaseline="middle" className="fill-fg text-[9px] font-semibold">
          {center ?? "—"}
        </text>
      </svg>
      <ul className="w-full space-y-1 text-sm" aria-label="Legenda composizione">
        {slices.map((s) => (
          <li key={s.key} className="flex items-center justify-between gap-3">
            <span className="flex min-w-0 items-center gap-2">
              <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: s.color }} aria-hidden />
              <span className="truncate">{s.label}</span>
            </span>
            <Pct value={s.weight_pct} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function Bars({ title, groups }: { title: string; groups: CompositionGroup[] }) {
  const max = groups.reduce((m, g) => Math.max(m, magnitude(g.value_eur)), 0);
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium text-muted">{title}</h3>
      <ul className="space-y-2 text-sm" aria-label={title}>
        {groups.map((g) => (
          <li key={g.key}>
            <div className="flex justify-between gap-3">
              <span>{g.label}</span>
              <span className="flex gap-3">
                <Money value={g.value_eur} />
                <span className="w-14 text-right">
                  <Pct value={g.weight_pct} />
                </span>
              </span>
            </div>
            <div className="mt-1 h-1.5 rounded bg-border">
              <div
                className={g.key === "cash" ? "h-1.5 rounded bg-muted" : "h-1.5 rounded bg-accent"}
                style={{ width: `${max > 0 ? (magnitude(g.value_eur) / max) * 100 : 0}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

const columns: Column<CompositionPosition>[] = [
  {
    key: "symbol",
    header: "Titolo",
    sortValue: (p) => p.symbol,
    cell: (p) => (
      <span className="flex flex-col">
        <span className="font-medium">
          {p.symbol}
          {p.stale && <span className="ml-2 text-xs text-warn">quotazione non aggiornata</span>}
          {p.value_eur == null && <span className="ml-2 text-xs text-warn">nessuna quotazione</span>}
        </span>
        <span className="text-xs text-muted">
          {p.name} · {assetClassLabel(p.asset_class)} · {p.market}
        </span>
      </span>
    ),
  },
  { key: "qty", header: "Quantità", align: "right", cell: (p) => <span className="num">{formatQty(p.qty)}</span> },
  { key: "avg", header: "Prezzo medio", align: "right", cell: (p) => <Money value={p.avg_price} currency={p.currency} /> },
  { key: "last", header: "Prezzo", align: "right", cell: (p) => <Money value={p.last} currency={p.currency} /> },
  { key: "value", header: "Valore (EUR)", align: "right", cell: (p) => <Money value={p.value_eur} /> },
  { key: "weight", header: "Peso", align: "right", cell: (p) => <Pct value={p.weight_pct} /> },
  { key: "pnl", header: "P&L", align: "right", cell: (p) => <Pnl value={p.pnl_eur} pct={p.pnl_pct} /> },
];

/** «Composizione» card of the portfolio detail: donut, groups, positions table. */
export function PortfolioComposition({ portfolioId }: { portfolioId: string }) {
  const { data, isLoading, isError, error } = useComposition(portfolioId);
  const cash = data?.by_asset_class.find((g) => g.key === "cash");
  const groups = data?.by_asset_class ?? [];

  return (
    <Card role="region" aria-label="Composizione">
      <CardHeader className="flex flex-row items-baseline justify-between gap-2 space-y-0">
        <CardTitle>Composizione</CardTitle>
        {data && <span className="text-xs text-muted">aggiornata {formatDateTime(data.as_of)}</span>}
      </CardHeader>
      <CardContent>
        {isError ? (
          <p className="text-sm text-loss">{errorMessage(error, "Composizione non disponibile")}</p>
        ) : !data ? (
          <p className="text-sm text-muted">{isLoading ? "Caricamento…" : "—"}</p>
        ) : data.positions.length === 0 ? (
          <p className="text-sm">
            <span className="font-medium">Solo contante</span>: <Money value={data.cash_eur} />
          </p>
        ) : (
          <div className="space-y-6">
            {data.unpriced_symbols.length > 0 && (
              <p className="text-sm text-warn">
                Senza quotazione: {data.unpriced_symbols.join(", ")}. Valore e pesi escludono queste posizioni.
              </p>
            )}
            <div className="grid gap-6 lg:grid-cols-3">
              <Donut
                slices={donutSlices(data.positions, cash)}
                center={data.equity_eur == null ? null : formatDecimal(data.equity_eur, 0) + " €"}
              />
              <Bars title="Per mercato/tipo" groups={groups} />
              <Bars title="Per valuta" groups={data.by_currency} />
            </div>
            <DataTable aria-label="Composizione per titolo" columns={columns} rows={data.positions} rowKey={(p) => p.symbol} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
