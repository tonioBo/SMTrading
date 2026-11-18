import { Link } from "react-router-dom";
import { useSignals } from "@/api/hooks/signals";
import type { SignalSymbol, StrategySignal } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { ExitBadge, SignalBadge } from "@/components/SignalBadges";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDecimal, formatQty } from "@/lib/format";
import { DIRECTION_LABELS, STRATEGY_LABELS } from "@/lib/signals";

const STATUS_TONE = { ok: "ok", "dati incompleti": "warn", "non supportata": "neutral" } as const satisfies Record<string, StatusTone>;

/** 2 decimals, 4 below 10 in absolute value (FX prices, small distances). */
function value(v: string | null): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  return formatDecimal(v, Number.isFinite(n) && Math.abs(n) < 10 ? 4 : 2);
}

function paramsText(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  return entries.length === 0 ? "—" : entries.map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(", ");
}

/** Rules whose threshold is a parameter already written in the rule (RSI, ROC levels). */
const LEVEL_RULES = new Set(["rsi_reversion", "momentum"]);

/** «uscita: RSI 2 sopra 70 · attuale 8,0» (RSI / ROC with one decimal, prices like the table). */
function exitText(type: string | null, s: SignalSymbol): string | null {
  if (!s.position || !s.exit) return null;
  const level = type != null && LEVEL_RULES.has(type);
  const current = level ? formatDecimal(s.exit.value, 1) : value(s.exit.value);
  const threshold = level ? "" : ` (${value(s.exit.threshold)})`;
  return `uscita: ${s.exit.rule}${threshold} · attuale ${current}`;
}

/** «filtro: Chiusura sopra SMA 200 (183,20) · attuale 190,00 · ok», «filtro: ATR 14 al
 * massimo 3% del prezzo · attuale 4,20% · blocca l'ingresso» per entry filter. */
function filterTexts(s: SignalSymbol): { text: string; passed: boolean }[] {
  return (s.filters ?? []).map((f) => {
    const pct = f.rule.startsWith("ATR");
    const threshold = pct ? "" : ` (${value(f.threshold)})`;
    const current = pct ? (f.value == null || f.value === "" ? "—" : `${formatDecimal(f.value, 2)}%`) : value(f.value);
    return { text: `filtro: ${f.rule}${threshold} · attuale ${current} · ${f.passed ? "ok" : "blocca l'ingresso"}`, passed: f.passed };
  });
}

function SymbolRow({ s, type }: { s: SignalSymbol; type: string | null }) {
  const exit = exitText(type, s);
  const filters = filterTexts(s);
  return (
    <TableRow>
      <TableCell className="font-medium">{s.symbol}</TableCell>
      <TableCell className="num text-right">{s.last == null || s.last === "" ? "—" : formatQty(s.last)}</TableCell>
      <TableCell>{s.indicator ?? "—"}</TableCell>
      <TableCell className="num text-right">{value(s.value_closed)}</TableCell>
      <TableCell className="num text-right">{value(s.value_now)}</TableCell>
      <TableCell className="num">{s.direction == null ? value(s.threshold) : `${DIRECTION_LABELS[s.direction] ?? s.direction} ${value(s.threshold)}`}</TableCell>
      <TableCell className="num text-right">{value(s.distance)}</TableCell>
      <TableCell>
        <div className="flex flex-col items-start gap-1">
          <SignalBadge s={s} type={type} />
          {filters.map((f) => (
            <span key={f.text} className={`num whitespace-nowrap text-xs ${f.passed ? "text-muted" : "text-warn"}`}>
              {f.text}
            </span>
          ))}
          {exit && (
            <div className="flex flex-wrap items-center gap-1">
              <span className="num whitespace-nowrap text-xs text-muted">{exit}</span>
              <ExitBadge type={type} s={s} />
            </div>
          )}
        </div>
      </TableCell>
      <TableCell className="whitespace-nowrap text-muted">{s.as_of ? formatDateTime(s.as_of) : "—"}</TableCell>
    </TableRow>
  );
}

function StrategyBlock({ strategy }: { strategy: StrategySignal }) {
  return (
    <section aria-label={`Strategia ${strategy.ref}`} className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="num font-semibold">{strategy.ref}</span>
        <span>{strategy.name}</span>
        <span className="text-muted" title={strategy.type ?? undefined}>
          {strategy.type == null ? "—" : (STRATEGY_LABELS[strategy.type] ?? strategy.type)}
        </span>
        <span className="num text-muted">{paramsText(strategy.params)}</span>
        <StatusBadge tone="neutral">{strategy.timeframe ?? "—"}</StatusBadge>
        {strategy.status !== "ok" && <StatusBadge tone={STATUS_TONE[strategy.status] ?? "neutral"}>{strategy.status}</StatusBadge>}
      </div>
      {strategy.symbols.length > 0 && (
        <Table aria-label={`Segnali ${strategy.ref}`}>
          <TableHeader>
            <TableRow>
              <TableHead>Simbolo</TableHead>
              <TableHead className="text-right">Ultimo</TableHead>
              <TableHead>Indicatore</TableHead>
              <TableHead className="text-right">Chiusura</TableHead>
              <TableHead className="text-right">Se chiudesse ora</TableHead>
              <TableHead>Soglia</TableHead>
              <TableHead className="text-right">Distanza</TableHead>
              <TableHead>Stato</TableHead>
              <TableHead>Aggiornato</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {strategy.symbols.map((s) => (
              <SymbolRow key={s.symbol} s={s} type={strategy.type} />
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

export default function Signals() {
  const { data, isError, isLoading } = useSignals();
  const portfolios = data?.portfolios ?? [];
  return (
    <div>
      <PageHeader title="Segnali">
        {data?.as_of && <span className="text-xs text-muted">{`ultimo aggiornamento ${formatDateTime(data.as_of)}`}</span>}
      </PageHeader>
      {isError && <p className="text-loss">Impossibile caricare i segnali</p>}
      {isLoading ? (
        <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        data &&
        (portfolios.length === 0 ? (
          <p className="text-sm text-muted">Nessuna strategia approvata</p>
        ) : (
          <div className="space-y-6">
            {portfolios.map((p) => (
              <Card key={p.portfolio_id ?? "none"}>
                <CardHeader>
                  <CardTitle>
                    {p.portfolio_id == null ? (
                      "Senza portafoglio"
                    ) : (
                      <Link to={`/portafogli/${p.portfolio_id}`} className="text-accent hover:underline">
                        {p.portfolio_name}
                      </Link>
                    )}
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                  {p.strategies.map((s) => (
                    <StrategyBlock key={s.ref} strategy={s} />
                  ))}
                </CardContent>
              </Card>
            ))}
          </div>
        ))
      )}
    </div>
  );
}
