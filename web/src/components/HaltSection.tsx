import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useHalts, useSetHalt } from "@/api/hooks/halts";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { TradingHalt } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { RevokeHaltButton } from "@/components/HaltBanner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { AdminOnly } from "@/components/AdminOnly";

const REASON_MAX = 500;
const GLOBAL = "";

function where(h: TradingHalt): string {
  return h.scope === "global" ? "Tutti i portafogli" : (h.portfolio ?? "Portafoglio");
}

function HaltForm() {
  const { data: portfolios } = usePortfolios();
  const targets = portfolios?.filter((p) => p.status !== "archived");
  const [target, setTarget] = useState(GLOBAL);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useSetHalt();
  const serverField = errorField(save.error);
  const reasonError = error ?? (save.error && serverField === "reason" ? errorMessage(save.error) : null);

  function submit(e: FormEvent) {
    e.preventDefault();
    const text = reason.trim();
    if (!text) return setError("Il motivo è obbligatorio");
    if (text.length > REASON_MAX) return setError(`Massimo ${REASON_MAX} caratteri`);
    setError(null);
    save.mutate(
      target === GLOBAL ? { scope: "global", reason: text } : { scope: "portfolio", portfolio_id: target, reason: text },
      {
        onSuccess: (res) => {
          setReason("");
          toast.success(res.canceled ? `Blocco aperture attivato (${res.canceled} ordini annullati)` : "Blocco aperture attivato");
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_auto] sm:items-end">
      <FormField label="Ambito">
        <select className={selectClass} value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value={GLOBAL}>Tutti i portafogli</option>
          {targets?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </FormField>
      <FormField label="Motivo" error={reasonError}>
        <input className={selectClass} value={reason} maxLength={REASON_MAX} onChange={(e) => setReason(e.target.value)} />
      </FormField>
      <Button type="submit" variant="destructive" disabled={save.isPending}>
        Sospendi aperture
      </Button>
      {save.error && serverField !== "reason" && (
        <p role="alert" className="text-sm text-loss sm:col-span-3">
          {errorMessage(save.error)}
        </p>
      )}
    </form>
  );
}

/** Rischio → «Blocco aperture»: set a halt, revoke the active ones, the history. */
export function HaltSection() {
  const { data, isError } = useHalts();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Blocco aperture</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted">
          Finché un blocco è attivo gli ordini che aumentano l&apos;esposizione sono rifiutati (TRADING_HALTED); chiusure, riduzioni, stop loss e take profit
          continuano. Di solito lo imposta il CEO via MCP.
        </p>
        <AdminOnly>
          <HaltForm />
        </AdminOnly>
        {isError && <p className="text-loss">Impossibile caricare i blocchi</p>}
        {data && (
          <>
            <div>
              <h3 className="mb-2 font-medium">Attivi</h3>
              {data.active.length === 0 ? (
                <p className="text-muted">Nessun blocco attivo</p>
              ) : (
                <ul className="space-y-2" aria-label="Blocchi attivi">
                  {data.active.map((h) => (
                    <li key={h.id} className="flex flex-wrap items-center gap-3 rounded-md border border-loss/40 px-3 py-2">
                      <span className="flex-1">
                        <span className="font-medium">{where(h)}</span> — {h.reason}
                        <span className="block text-xs text-muted">
                          dal {formatDateTime(h.set_at)} · {h.set_by_name ?? h.set_by}
                        </span>
                      </span>
                      <RevokeHaltButton halt={h} />
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="overflow-x-auto">
              <h3 className="mb-2 font-medium">Storico</h3>
              {data.history.length === 0 ? (
                <p className="text-muted">Nessun blocco revocato</p>
              ) : (
                <table aria-label="Storico blocchi" className="w-full text-sm">
                  <thead className="text-left text-muted">
                    <tr>
                      <th className="py-1 pr-3 font-normal">Ambito</th>
                      <th className="py-1 pr-3 font-normal">Motivo</th>
                      <th className="py-1 pr-3 font-normal">Attivato</th>
                      <th className="py-1 font-normal">Revocato</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.history.map((h) => (
                      <tr key={h.id} className="border-t border-border align-top">
                        <td className="py-1.5 pr-3">{where(h)}</td>
                        <td className="py-1.5 pr-3">{h.reason}</td>
                        <td className="py-1.5 pr-3">
                          {formatDateTime(h.set_at)}
                          <span className="block text-xs text-muted">{h.set_by_name ?? h.set_by}</span>
                        </td>
                        <td className="py-1.5">
                          {h.cleared_at ? formatDateTime(h.cleared_at) : "—"}
                          <span className="block text-xs text-muted">{h.cleared_by_name ?? h.cleared_by}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
