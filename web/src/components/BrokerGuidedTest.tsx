import { useState, type FormEvent } from "react";
import { useGuidedBrokerTest } from "@/api/hooks/brokers";
import type { BrokerLink, GuidedTestStep } from "@/api/types";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

/** The second confirmation of the test order (brokers.guided_test.ORDER_CONFIRM). */
const GUIDED_ORDER_CONFIRM = "ORDINE DI PROVA";

const STEP_STATUS: Record<GuidedTestStep["status"], { text: string; className: string }> = {
  ok: { text: "OK", className: "text-gain" },
  error: { text: "Errore", className: "text-loss" },
  skipped: { text: "Saltato", className: "text-muted" },
};

/** «Prova collegamento» (admin, paper/demo only): the read-only checks step by step and,
 * with a second explicit confirmation, a test order canceled at once. */
export function GuidedTest({ link }: { link: BrokerLink }) {
  const run = useGuidedBrokerTest();
  const [open, setOpen] = useState(false);
  const [symbol, setSymbol] = useState("AAPL");
  const [withOrder, setWithOrder] = useState(false);
  const [confirm, setConfirm] = useState("");
  const confirmed = confirm.trim().toUpperCase() === GUIDED_ORDER_CONFIRM;
  function submit(e: FormEvent) {
    e.preventDefault();
    run.mutate({
      id: link.id,
      body: withOrder ? { symbol, order_test: true, confirm } : { symbol, order_test: false },
    });
  }
  if (!open) {
    return (
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}>
        Prova collegamento
      </Button>
    );
  }
  return (
    <form aria-label={`Prova collegamento ${link.name}`} onSubmit={submit} className="space-y-3 rounded-md border p-3">
      <p className="text-xs text-muted">
        Controlli in sola lettura: accesso, conto, potere d'acquisto, stato del mercato e una quotazione di esempio. Solo
        su conti paper/demo.
      </p>
      <FormField label="Strumento di esempio">
        <Input value={symbol} onChange={(e) => setSymbol(e.target.value)} className="max-w-[10rem]" />
      </FormField>
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" checked={withOrder} onChange={(e) => setWithOrder(e.target.checked)} className="mt-1" />
        <span>Includi un ordine di prova: acquisto limite di 1 unità a metà prezzo (non può essere eseguito), annullato subito</span>
      </label>
      {withOrder && (
        <FormField label={`Per confermare l'ordine di prova scrivi ${GUIDED_ORDER_CONFIRM}`}>
          <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />
        </FormField>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={run.isPending || !symbol.trim() || (withOrder && !confirmed)}>
          {run.isPending ? "Prova in corso…" : "Avvia prova"}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Chiudi
        </Button>
      </div>
      {run.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(run.error)}
        </p>
      )}
      {run.data && (
        <div className="space-y-1">
          <p role="status" className={run.data.ok ? "text-sm text-gain" : "text-sm text-loss"}>
            {run.data.ok ? "Prova completata senza errori" : "Prova completata con errori"}
          </p>
          <ol aria-label={`Esito della prova ${link.name}`} className="space-y-1 text-sm">
            {run.data.steps.map((s) => (
              <li key={s.key} className="flex flex-wrap gap-x-2">
                <span className={`font-medium ${STEP_STATUS[s.status].className}`}>{STEP_STATUS[s.status].text}</span>
                <span className="font-medium">{s.label}:</span>
                <span className="text-muted">{s.detail}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </form>
  );
}
