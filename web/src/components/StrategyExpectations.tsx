import { Link } from "react-router-dom";
import { useStrategyExpectations } from "@/api/hooks/strategies";
import type { ExpectationVerdict, StrategyExpectation } from "@/api/types";
import { Pnl } from "@/components/Pnl";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDecimal } from "@/lib/format";
import { formatDay } from "@/lib/goals";
import { journalLink } from "@/lib/trades";

const VERDICT_TONE: Record<ExpectationVerdict, StatusTone> = {
  "in linea": "ok",
  "sotto le attese": "danger",
  "dati insufficienti": "neutral",
};
const SOURCE_LABELS: Record<string, string> = { approvazione: "dall'approvazione", quant: "dal Quant", backtest: "dal backtest" };

function pct(v: string | null, places = 1): string {
  return v == null ? "—" : `${formatDecimal(v, places)}%`;
}

function num(v: string | null, places = 1): string {
  return v == null ? "—" : formatDecimal(v, places);
}

/** «previsto · reale» in one cell. */
function Pair({ expected, live }: { expected: string; live: string }) {
  return (
    <span className="num whitespace-nowrap">
      <span className="text-muted">{expected}</span>
      {" · "}
      <span className="font-medium">{live}</span>
    </span>
  );
}

function Verdict({ s }: { s: StrategyExpectation }) {
  return (
    <div>
      <StatusBadge tone={VERDICT_TONE[s.verdict]}>{s.verdict}</StatusBadge>
      {s.reasons.map((r) => (
        <div key={r} className="mt-1 text-xs text-muted">
          {r}
        </div>
      ))}
    </div>
  );
}

function Period({ s }: { s: StrategyExpectation }) {
  const e = s.expected;
  return (
    <div className="text-xs text-muted">
      <div>{`approvata il ${formatDateTime(s.approved_at)} · ${s.live.days} giorni`}</div>
      {e.from_date && e.to_date && <div>{`backtest ${formatDay(e.from_date)} – ${formatDay(e.to_date)}`}</div>}
      {e.source && <div>{`metriche ${SOURCE_LABELS[e.source] ?? e.source}`}</div>}
    </div>
  );
}

function Name({ s }: { s: StrategyExpectation }) {
  return (
    <div>
      <Link to={journalLink({ strategy: s.ref })} className="num font-semibold text-accent hover:underline">
        {s.ref}
      </Link>
      <div className="text-xs text-muted">{`${s.name} · ${s.status}`}</div>
    </div>
  );
}

const ROWS: { label: string; expected: (s: StrategyExpectation) => string; live: (s: StrategyExpectation) => string }[] = [
  { label: "Operazioni al mese", expected: (s) => num(s.expected.trades_per_month, 2), live: (s) => num(s.live.trades_per_month, 2) },
  { label: "Vincenti", expected: (s) => pct(s.expected.win_rate_pct), live: (s) => pct(s.live.win_rate_pct) },
  { label: "Media per operazione", expected: (s) => pct(s.expected.avg_trade_pct, 2), live: (s) => pct(s.live.avg_trade_pct, 2) },
  { label: "Max drawdown", expected: (s) => pct(s.expected.max_drawdown_pct), live: (s) => pct(s.live.max_drawdown_pct) },
];

/** «Strategie» → «Previsto contro reale»: the backtest's promise against the live trades. */
export function StrategyExpectations() {
  const { data, isError, isLoading } = useStrategyExpectations();
  const rows = data?.strategies ?? [];
  if (isError) return <p className="text-loss">Impossibile caricare il confronto con il backtest</p>;
  if (isLoading) return <p className="text-sm text-muted">Caricamento…</p>;
  if (!data) return null;
  if (rows.length === 0) return <p className="text-sm text-muted">Nessuna strategia approvata</p>;
  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <p className="text-xs text-muted">
          {`Per ogni strategia approvata: in grigio quanto prometteva il backtest, in evidenza quanto ha fatto dalle operazioni aperte dopo l'approvazione (al netto dei costi). Il giudizio arriva con almeno ${data.min_trades} operazioni chiuse.`}
        </p>
        <div className="hidden md:block">
          <Table aria-label="Previsto contro reale">
            <TableHeader>
              <TableRow>
                <TableHead>Strategia</TableHead>
                <TableHead className="text-right">Operazioni</TableHead>
                {ROWS.map((r) => (
                  <TableHead key={r.label} className="text-right">
                    {r.label}
                  </TableHead>
                ))}
                <TableHead className="text-right">Risultato</TableHead>
                <TableHead>Giudizio</TableHead>
                <TableHead>Periodo</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((s) => (
                <TableRow key={s.ref}>
                  <TableCell>
                    <Name s={s} />
                  </TableCell>
                  <TableCell className="num text-right">
                    <Pair expected={s.expected.trades == null ? "—" : String(s.expected.trades)} live={`${s.live.closed_trades}/${s.live.trades}`} />
                  </TableCell>
                  {ROWS.map((r) => (
                    <TableCell key={r.label} className="text-right">
                      <Pair expected={r.expected(s)} live={r.live(s)} />
                    </TableCell>
                  ))}
                  <TableCell className="text-right">
                    <Pnl value={s.live.pnl_eur} />
                  </TableCell>
                  <TableCell>
                    <Verdict s={s} />
                  </TableCell>
                  <TableCell>
                    <Period s={s} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-2 text-xs text-muted">Operazioni: previste nel backtest · chiuse/aperte dall'approvazione.</p>
        </div>
        <ul className="space-y-3 md:hidden" aria-label="Previsto contro reale (schede)">
          {rows.map((s) => (
            <li key={s.ref} className="rounded-lg border border-border p-3">
              <div className="flex items-start justify-between gap-2">
                <Name s={s} />
                <StatusBadge tone={VERDICT_TONE[s.verdict]}>{s.verdict}</StatusBadge>
              </div>
              <dl className="mt-2 grid grid-cols-[1fr_auto_auto] gap-x-3 gap-y-1 text-sm">
                <dt />
                <dd className="text-xs text-muted">previsto</dd>
                <dd className="text-xs text-muted">reale</dd>
                {ROWS.map((r) => (
                  <div key={r.label} className="contents">
                    <dt className="text-muted">{r.label}</dt>
                    <dd className="num text-right text-muted">{r.expected(s)}</dd>
                    <dd className="num text-right font-medium">{r.live(s)}</dd>
                  </div>
                ))}
              </dl>
              {s.reasons.map((r) => (
                <p key={r} className="mt-1 text-xs text-muted">
                  {r}
                </p>
              ))}
              <div className="mt-2">
                <Period s={s} />
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
