import { Fragment, useCallback, useMemo, useState, type CSSProperties } from "react";
import { useResearchRun, useResearchRuns } from "@/api/hooks/strategies";
import type { ParamValue, ResearchEquityPoint, ResearchKind, ResearchMetrics, ResearchRun, ResearchStatus, WalkForwardVerdict } from "@/api/types";
import { EquityChart } from "@/components/charts";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SERIES_COLORS } from "@/lib/equityHistory";
import { formatDateTime, formatDecimal } from "@/lib/format";
import { formatDay } from "@/lib/goals";
import { chartNumber, nearestIndex, type ChartLine } from "@/lib/lineChart";

const KIND_LABELS: Record<ResearchKind, string> = {
  walk_forward: "Walk-forward",
  param_sweep: "Sweep parametri",
  portfolio: "Portafoglio di strategie",
};
const STATUS: Record<ResearchStatus, [string, StatusTone]> = {
  queued: ["in coda", "neutral"],
  running: ["in corso", "info"],
  completed: ["completata", "ok"],
  failed: ["fallita", "danger"],
};
const VERDICT_TONE: Record<WalkForwardVerdict, StatusTone> = {
  solida: "ok",
  fragile: "warn",
  "da scartare": "danger",
  "dati insufficienti": "neutral",
};

function pct(v: string | number | null | undefined, places = 1): string {
  return v == null ? "—" : `${formatDecimal(String(v), places)}%`;
}

function num(v: string | number | null | undefined, places = 2): string {
  return v == null ? "—" : formatDecimal(String(v), places);
}

function params(p: Record<string, ParamValue> | undefined): string {
  const entries = Object.entries(p ?? {});
  return entries.length === 0 ? "—" : entries.map(([k, v]) => `${k} ${v}`).join(" · ");
}

function strategyOf(r: ResearchRun): string {
  if (r.kind !== "portfolio") return r.strategy ?? "—";
  return (r.spec.sleeves ?? []).map((s) => s.strategy).join(" + ") || "—";
}

/** The headline of a completed run: the verdict, the best robust params, the return. */
function Outcome({ r }: { r: ResearchRun }) {
  const s = r.summary;
  if (r.status === "failed") return <span className="text-xs text-loss">{r.error ?? "errore"}</span>;
  if (!s) return <span className="text-muted">—</span>;
  if (r.kind === "walk_forward" && s.verdict)
    return (
      <div>
        <StatusBadge tone={VERDICT_TONE[s.verdict]}>{s.verdict}</StatusBadge>
        <div className="num mt-1 text-xs text-muted">{`fuori campione ${pct(s.oos_metrics?.total_return_pct)} · DD ${pct(s.oos_metrics?.max_drawdown_pct)} · stabilità ${pct(s.stability_pct, 0)}`}</div>
      </div>
    );
  if (r.kind === "param_sweep")
    return (
      <div className="text-xs">
        <div>{`più robusti: ${params(s.best_by_robust)}`}</div>
        {s.isolated_peak && <div className="text-muted">{`migliore per metrica (picco isolato): ${params(s.best_by_metric)}`}</div>}
      </div>
    );
  return <span className="num text-xs">{`${pct(s.metrics?.total_return_pct)} · DD ${pct(s.metrics?.max_drawdown_pct)} · Sharpe ${num(s.metrics?.sharpe)}`}</span>;
}

function Curve({ id, points, daily, label }: { id: string; points: ResearchEquityPoint[]; daily: boolean; label: string }) {
  const lines: ChartLine[] = useMemo(
    () => [{ key: "equity", label: "Equity", color: SERIES_COLORS[0], points: points.map((p) => ({ t: Date.parse(p.ts), v: Number(p.equity) })) }],
    [points],
  );
  const rowsAt = useCallback(
    (t: number) => {
      const pts = lines[0].points;
      const i = nearestIndex(pts, t);
      if (i < 0) return [];
      const dd = points[i].drawdown_pct;
      return [{ key: "equity", label: "Equity", color: lines[0].color, value: `${chartNumber(pts[i].v, 2)} €`, change: dd == null ? null : `drawdown ${pct(dd)}` }];
    },
    [lines, points],
  );
  if (points.length < 2) return null;
  return <EquityChart lines={lines} single formatValue={(v) => `${chartNumber(v, 0)} €`} rowsAt={rowsAt} daily={daily} ariaLabel={label} resetKey={id} />;
}

function MetricCells({ m }: { m: ResearchMetrics | undefined }) {
  return (
    <>
      <TableCell className="num text-right">{pct(m?.total_return_pct)}</TableCell>
      <TableCell className="num text-right">{pct(m?.max_drawdown_pct)}</TableCell>
      <TableCell className="num text-right">{num(m?.sharpe)}</TableCell>
      <TableCell className="num text-right">{m?.trades ?? "—"}</TableCell>
    </>
  );
}

function Detail({ id }: { id: string }) {
  const { data: run, isError, isLoading } = useResearchRun(id);
  if (isError) return <p className="text-loss">Impossibile caricare l'analisi</p>;
  if (isLoading || !run) return <p className="text-sm text-muted">Caricamento…</p>;
  const res = run.result;
  if (!res) return <p className="text-sm text-muted">{run.error ?? "Risultato non ancora disponibile"}</p>;
  const daily = run.timeframe === "1d" || run.timeframe === "1w";
  return (
    <div className="space-y-3">
      {res.reasons && (
        <ul className="list-disc pl-5 text-xs text-muted">
          {res.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      {res.windows && (
        <>
          <Table aria-label="Finestre del walk-forward">
            <TableHeader>
              <TableRow>
                <TableHead>Finestra</TableHead>
                <TableHead>Fuori campione</TableHead>
                <TableHead>Parametri scelti</TableHead>
                <TableHead className="text-right">Rend. in campione</TableHead>
                <TableHead className="text-right">Rend. fuori</TableHead>
                <TableHead className="text-right">DD fuori</TableHead>
                <TableHead className="text-right">Sharpe fuori</TableHead>
                <TableHead className="text-right">Operazioni</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {res.windows.map((w) => (
                <TableRow key={w.window}>
                  <TableCell className="num">{w.window}</TableCell>
                  <TableCell className="whitespace-nowrap text-xs">{`${formatDay(w.oos_from)} – ${formatDay(w.oos_to)}`}</TableCell>
                  <TableCell className="text-xs">{params(w.best_params)}</TableCell>
                  <TableCell className="num text-right">{pct(w.is_metrics.total_return_pct)}</TableCell>
                  <MetricCells m={w.oos_metrics} />
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Curve id={run.id} points={res.oos_equity ?? []} daily={daily} label="Equity fuori campione" />
        </>
      )}
      {res.rows && (
        <>
          {res.heatmap && (
            <Table aria-label="Mappa dei parametri">
              <TableHeader>
                <TableRow>
                  <TableHead>{`${res.heatmap.y_param} \\ ${res.heatmap.x_param}`}</TableHead>
                  {res.heatmap.x_values.map((x) => (
                    <TableHead key={String(x)} className="text-right">
                      {String(x)}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {res.heatmap.y_values.map((y, yi) => (
                  <TableRow key={String(y)}>
                    <TableCell className="num font-medium">{String(y)}</TableCell>
                    {res.heatmap!.z[yi].map((v, xi) => (
                      <TableCell key={xi} className="num text-right" style={heat(v, res.heatmap!.z)}>
                        {num(v)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <Table aria-label="Combinazioni migliori">
            <TableHeader>
              <TableRow>
                <TableHead>#</TableHead>
                <TableHead>Parametri</TableHead>
                <TableHead className="text-right">Rendimento</TableHead>
                <TableHead className="text-right">Max DD</TableHead>
                <TableHead className="text-right">Sharpe</TableHead>
                <TableHead className="text-right">Operazioni</TableHead>
                <TableHead className="text-right">Robustezza</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {res.rows.slice(0, 10).map((r) => (
                <TableRow key={r.rank}>
                  <TableCell className="num">{r.rank}</TableCell>
                  <TableCell className="text-xs">{params(r.params)}</TableCell>
                  <MetricCells m={r.metrics} />
                  <TableCell className="num text-right">{num(r.robust)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
      {res.contribution && (
        <>
          <Curve id={run.id} points={res.equity ?? []} daily={daily} label="Equity del portafoglio di strategie" />
          <Table aria-label="Contributo per strategia">
            <TableHeader>
              <TableRow>
                <TableHead>Strategia</TableHead>
                <TableHead>Simboli</TableHead>
                <TableHead className="text-right">Risultato</TableHead>
                <TableHead className="text-right">Contributo</TableHead>
                <TableHead className="text-right">Operazioni</TableHead>
                <TableHead className="text-right">Vincenti</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {res.contribution.map((c) => (
                <TableRow key={c.sleeve}>
                  <TableCell className="num">{`${c.sleeve}:${c.strategy}`}</TableCell>
                  <TableCell className="text-xs">{c.symbols.join(", ")}</TableCell>
                  <TableCell className="num text-right">{num(c.pnl)}</TableCell>
                  <TableCell className="num text-right">{pct(c.contribution_pct, 2)}</TableCell>
                  <TableCell className="num text-right">{c.trades}</TableCell>
                  <TableCell className="num text-right">{pct(c.win_rate_pct)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {res.correlation && res.correlation.labels.length > 1 && (
            <Table aria-label="Correlazione tra strategie">
              <TableHeader>
                <TableRow>
                  <TableHead />
                  {res.correlation.labels.map((l) => (
                    <TableHead key={l} className="text-right">
                      {l}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {res.correlation.matrix.map((row, i) => (
                  <TableRow key={res.correlation!.labels[i]}>
                    <TableCell className="num font-medium">{res.correlation!.labels[i]}</TableCell>
                    {row.map((v, j) => (
                      <TableCell key={j} className="num text-right">
                        {num(v)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </div>
  );
}

/** A cell's background on the green scale of the heatmap (its rank among the values). */
function heat(v: string | null, z: (string | null)[][]): CSSProperties | undefined {
  if (v == null) return undefined;
  const all = z.flat().filter((x): x is string => x != null).map(Number);
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const share = hi > lo ? (Number(v) - lo) / (hi - lo) : 0.5;
  return { backgroundColor: `rgba(16, 185, 129, ${(0.08 + share * 0.42).toFixed(2)})` };
}

/** «Strategie» → «Ricerca»: the Quant's walk-forward, sweeps and portfolio backtests. */
export function StrategyResearch() {
  const { data, isError, isLoading } = useResearchRuns();
  const [open, setOpen] = useState<string | null>(null);
  if (isError) return <p className="text-loss">Impossibile caricare le analisi</p>;
  if (isLoading) return <p className="text-sm text-muted">Caricamento…</p>;
  const runs = data?.runs ?? [];
  if (runs.length === 0) return <p className="text-sm text-muted">Nessuna analisi di ricerca</p>;
  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <p className="text-xs text-muted">
          Analisi del Quant sullo storico, con gli stessi costi dei backtest: walk-forward (parametri scelti su un tratto e verificati sul successivo), sweep dei parametri (con la robustezza: mediana delle combinazioni vicine) e portafogli di più strategie con un capitale comune.
        </p>
        <div className="overflow-x-auto">
          <Table aria-label="Analisi di ricerca">
            <TableHeader>
              <TableRow>
                <TableHead>Tipo</TableHead>
                <TableHead>Strategia</TableHead>
                <TableHead>Simboli</TableHead>
                <TableHead>Periodo</TableHead>
                <TableHead>Stato</TableHead>
                <TableHead>Esito</TableHead>
                <TableHead>Creata</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((r) => {
                const [label, tone] = STATUS[r.status];
                const isOpen = open === r.id;
                return (
                  <Fragment key={r.id}>
                    <TableRow>
                      <TableCell className="whitespace-nowrap">{KIND_LABELS[r.kind]}</TableCell>
                      <TableCell className="num">{strategyOf(r)}</TableCell>
                      <TableCell className="text-xs">{r.symbols.join(", ")}</TableCell>
                      <TableCell className="whitespace-nowrap text-xs">{`${formatDay(r.from_date)} – ${formatDay(r.to_date)} · ${r.timeframe}`}</TableCell>
                      <TableCell>
                        <StatusBadge tone={tone}>{label}</StatusBadge>
                      </TableCell>
                      <TableCell>
                        <Outcome r={r} />
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted">
                        {formatDateTime(r.created_at)}
                        {r.agent && <div>{r.agent}</div>}
                      </TableCell>
                      <TableCell>
                        {r.status === "completed" && (
                          <Button variant="ghost" size="sm" aria-expanded={isOpen} aria-label={`Dettaglio analisi ${KIND_LABELS[r.kind]} ${strategyOf(r)}`} onClick={() => setOpen(isOpen ? null : r.id)}>
                            {isOpen ? "Chiudi" : "Dettaglio"}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                    {isOpen && (
                      <TableRow>
                        <TableCell colSpan={8} className="bg-card/50">
                          <Detail id={r.id} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
