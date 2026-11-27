import { useState, type FormEvent } from "react";
import { useAutoRuleBacktest } from "@/api/hooks/autoRules";
import type { AutoBacktestBody, AutoRule, AutoValue, ReplayResult, ReplayTrade } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { Pnl } from "@/components/Pnl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";
import { EXIT_REASON_LABELS, MODEL_LABELS, decimalText } from "@/lib/autoRules";
import { formatDateTime, formatQty } from "@/lib/format";

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

const columns = (currency: string): Column<ReplayTrade>[] => [
  { key: "symbol", header: "Simbolo", primary: true, cell: (t) => t.symbol },
  { key: "entry", header: "Entrata", cell: (t) => formatDateTime(t.entry_ts) },
  { key: "entry_price", header: "Prezzo d'entrata", align: "right", cell: (t) => <span className="num">{formatQty(t.entry_price)}</span> },
  { key: "exit", header: "Uscita", cell: (t) => formatDateTime(t.exit_ts) },
  { key: "exit_price", header: "Prezzo d'uscita", align: "right", cell: (t) => <span className="num">{formatQty(t.exit_price)}</span> },
  { key: "reason", header: "Motivo d'uscita", cell: (t) => EXIT_REASON_LABELS[t.exit_reason] ?? t.exit_reason },
  { key: "qty", header: "Pezzi", align: "right", cell: (t) => <span className="num">{formatQty(t.qty)}</span> },
  { key: "fees", header: "Commissioni", align: "right", cell: (t) => <Money value={t.fees} currency={currency} /> },
  { key: "net", header: "Netto", align: "right", cell: (t) => <Pnl value={t.net} currency={currency} /> },
];

function Result({ r, currency }: { r: ReplayResult; currency: string }) {
  return (
    <div className="space-y-3" aria-label="Risultato della prova">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-muted">Operazioni</dt>
          <dd className="num">{r.trades_total}</dd>
        </div>
        <div>
          <dt className="text-muted">Vinte / perse</dt>
          <dd className="num">
            {r.wins} / {r.losses}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Netto</dt>
          <dd>
            <Pnl value={r.net} currency={currency} />
          </dd>
        </div>
        <div>
          <dt className="text-muted">Commissioni</dt>
          <dd>
            <Money value={r.fees} currency={currency} />
          </dd>
        </div>
        <div>
          <dt className="text-muted">Calo massimo</dt>
          <dd>
            <Money value={r.max_drawdown} currency={currency} />
          </dd>
        </div>
      </dl>
      {r.notes.length > 0 && (
        <ul aria-label="Limiti della prova" className="list-disc space-y-0.5 rounded-md border border-warn/50 bg-warn/10 p-3 pl-7 text-sm">
          {r.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {r.truncated && (
        <p className="text-sm text-muted">
          Mostrate le ultime {r.trades.length} operazioni su {r.trades_total}: i totali le contano tutte.
        </p>
      )}
      <DataTable aria-label="Operazioni della prova" columns={columns(currency)} rows={r.trades} rowKey={(t) => `${t.symbol}-${t.entry_ts}-${t.exit_ts}`} empty="Nessuna operazione nel periodo" />
    </div>
  );
}

/** «Prova sullo storico»: the replay of a rule's model and parameters on one-minute history. */
export function AutoRuleBacktest({ rules }: { rules: AutoRule[] }) {
  const run = useAutoRuleBacktest();
  const today = new Date();
  const [ruleId, setRuleId] = useState(rules[0]?.id ?? "");
  const [from, setFrom] = useState(isoDay(new Date(today.getTime() - 7 * 86_400_000)));
  const [to, setTo] = useState(isoDay(today));
  const [spread, setSpread] = useState("0,05");
  const [delay, setDelay] = useState("1");
  const [fees, setFees] = useState<"broker" | "zero">("broker");
  const [error, setError] = useState<string | null>(null);
  const [currency, setCurrency] = useState("EUR");
  const rule = rules.find((r) => r.id === ruleId) ?? rules[0];

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!rule) return;
    const s = decimalText(spread);
    if (s === null || Number(s) > 5) return setError("Forbice: un numero da 0 a 5 (%)");
    if (!/^\d+$/.test(delay.trim()) || Number(delay) > 60) return setError("Ritardo: un numero intero di secondi, da 0 a 60");
    if (from === "" || to === "" || from > to) return setError("Date: indica il periodo, dal giorno iniziale al finale");
    setError(null);
    const body: AutoBacktestBody = {
      symbol: rule.symbol,
      model: rule.model,
      params: rule.params as Record<string, AutoValue>,
      from,
      to,
      spread_pct: s,
      delay_s: Number(delay),
      fee_rule: fees,
      ...(rule.qty != null ? { qty: rule.qty } : { notional: rule.notional ?? undefined }),
      ...(fees === "broker" ? { portfolio_id: rule.portfolio_id } : {}),
    };
    setCurrency(rule.today.net_currency);
    run.mutate(body);
  }

  if (rules.length === 0) return <p className="text-sm text-muted">Crea prima una regola: la prova usa il suo modello e i suoi parametri.</p>;
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">Rifà sullo storico al minuto le entrate e le uscite della regola scelta, con commissioni, forbice e ritardo. Al massimo 30 giorni.</p>
      <form onSubmit={submit} noValidate aria-label="Prova sullo storico" className="grid gap-3 sm:grid-cols-3">
        <FormField label="Regola">
          <select className={selectClass} value={rule?.id ?? ""} onChange={(e) => setRuleId(e.target.value)}>
            {rules.map((r) => (
              <option key={r.id} value={r.id}>
                {r.portfolio_name} · {r.symbol} · {MODEL_LABELS[r.model] ?? r.model}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Dal giorno">
          <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </FormField>
        <FormField label="Al giorno (compreso)">
          <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </FormField>
        <FormField label="Forbice (%)" hint="distanza tra prezzo di acquisto e di vendita">
          <Input value={spread} inputMode="decimal" onChange={(e) => setSpread(e.target.value)} />
        </FormField>
        <FormField label="Ritardo dell'ordine (secondi)" hint="da 0 a 60">
          <Input value={delay} inputMode="numeric" onChange={(e) => setDelay(e.target.value)} />
        </FormField>
        <FormField label="Commissioni">
          <select className={selectClass} value={fees} onChange={(e) => setFees(e.target.value as "broker" | "zero")}>
            <option value="broker">quelle del portafoglio</option>
            <option value="zero">nessuna commissione</option>
          </select>
        </FormField>
        {(error || run.error) && (
          <p role="alert" className="text-sm text-loss sm:col-span-3">
            {error ?? errorMessage(run.error)}
          </p>
        )}
        <div className="sm:col-span-3">
          <Button type="submit" disabled={run.isPending}>
            {run.isPending ? "Prova in corso…" : "Avvia la prova"}
          </Button>
        </div>
      </form>
      {run.data && <Result r={run.data} currency={currency} />}
    </div>
  );
}
