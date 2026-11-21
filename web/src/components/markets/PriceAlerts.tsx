import { BellRing, Pencil, RotateCcw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useOverview } from "@/api/hooks/marketsV2";
import { useCreatePriceAlert, useDeletePriceAlert, usePatchPriceAlert, usePriceAlerts } from "@/api/hooks/marketsV2";
import type { AlertDirection, PriceAlert } from "@/api/marketsV2Types";
import { FormField, selectClass } from "@/components/FormField";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { amountError, normalizeAmount, toInputDecimal } from "@/lib/decimal";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatQty } from "@/lib/format";

const CATALOG = "__catalog__";
const MAX_NOTE = 200;

function AlertForm() {
  const overview = useOverview();
  const create = useCreatePriceAlert();
  const [target, setTarget] = useState<string>(CATALOG);
  const [symbol, setSymbol] = useState("");
  const [direction, setDirection] = useState<AlertDirection>("above");
  const [threshold, setThreshold] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(e: FormEvent) {
    e.preventDefault();
    const sym = target === CATALOG ? symbol.trim().toUpperCase() : target;
    const value = normalizeAmount(threshold);
    if (!sym) return setError("Indica lo strumento");
    if (!value || Number(value) <= 0) return setError(amountError(threshold) ?? "La soglia deve essere positiva");
    if (note.trim().length > MAX_NOTE) return setError(`Nota: al massimo ${MAX_NOTE} caratteri`);
    setError(null);
    create.mutate(
      { symbol: sym, source: target === CATALOG ? "catalog" : "overview", direction, threshold: value, note: note.trim() },
      {
        onSuccess: () => {
          setThreshold("");
          setNote("");
        },
      },
    );
  }

  const current = target !== CATALOG ? overview.data?.items.find((i) => i.code === target) : undefined;
  return (
    <form aria-label="Nuovo avviso di prezzo" onSubmit={submit} noValidate className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
      <FormField label="Su">
        <select className={selectClass} value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value={CATALOG}>Strumento del catalogo…</option>
          {(overview.data?.items ?? []).map((i) => (
            <option key={i.code} value={i.code}>
              {i.label}
            </option>
          ))}
        </select>
      </FormField>
      {target === CATALOG ? (
        <FormField label="Simbolo">
          <Input value={symbol} placeholder="es. AAPL" onChange={(e) => setSymbol(e.target.value)} />
        </FormField>
      ) : (
        <div className="self-center text-xs text-muted">{current?.value ? `Ora ${formatQty(current.value)}` : ""}</div>
      )}
      <FormField label="Condizione">
        <select className={selectClass} value={direction} onChange={(e) => setDirection(e.target.value as AlertDirection)}>
          <option value="above">Sale sopra</option>
          <option value="below">Scende sotto</option>
        </select>
      </FormField>
      <FormField label="Soglia">
        <Input value={threshold} inputMode="decimal" placeholder="es. 7.000" onChange={(e) => setThreshold(e.target.value)} />
      </FormField>
      <FormField label="Nota (facoltativa)">
        <Input value={note} maxLength={MAX_NOTE} onChange={(e) => setNote(e.target.value)} />
      </FormField>
      {(error || create.error) && (
        <p role="alert" className="text-sm text-loss sm:col-span-2 lg:col-span-5">
          {error ?? errorMessage(create.error)}
        </p>
      )}
      <div className="sm:col-span-2 lg:col-span-5">
        <Button type="submit" size="sm" disabled={create.isPending}>
          <BellRing className="mr-1 h-4 w-4" />
          Crea avviso
        </Button>
      </div>
    </form>
  );
}

function AlertRow({ a }: { a: PriceAlert }) {
  const patch = usePatchPriceAlert();
  const remove = useDeletePriceAlert();
  const [editing, setEditing] = useState(false);
  const [threshold, setThreshold] = useState(toInputDecimal(a.threshold));
  const save = (e: FormEvent) => {
    e.preventDefault();
    const v = normalizeAmount(threshold);
    if (!v || Number(v) <= 0) return;
    patch.mutate({ id: a.id, threshold: v }, { onSuccess: () => setEditing(false) });
  };
  return (
    <li className="flex flex-wrap items-center gap-2 py-2 text-sm">
      <Badge variant={a.status === "active" ? "default" : "secondary"}>{a.status === "active" ? "Attivo" : "Scattato"}</Badge>
      <span className="font-medium">{a.label}</span>
      {editing ? (
        <form onSubmit={save} className="flex items-center gap-1">
          <span>{a.direction === "above" ? "sopra" : "sotto"}</span>
          <Input aria-label={`Nuova soglia per ${a.label}`} className="h-8 w-28" value={threshold} inputMode="decimal" onChange={(e) => setThreshold(e.target.value)} autoFocus />
          <Button type="submit" size="sm" disabled={patch.isPending}>
            Salva
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
            Annulla
          </Button>
        </form>
      ) : (
        <span>
          {a.direction === "above" ? "sopra" : "sotto"} <span className="num">{formatQty(a.threshold)}</span>
        </span>
      )}
      {a.note && <span className="text-muted">· {a.note}</span>}
      {a.triggered_at && (
        <span className="text-xs text-muted">
          scattato {formatDateTime(a.triggered_at).slice(0, 16)}
          {a.triggered_price ? ` a ${formatQty(a.triggered_price)}` : ""}
        </span>
      )}
      <span className="ml-auto flex gap-1">
        {!editing && (
          <Button type="button" size="sm" variant="ghost" aria-label={`Modifica l'avviso su ${a.label}`} onClick={() => setEditing(true)}>
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        )}
        {a.status === "triggered" && (
          <Button type="button" size="sm" variant="ghost" aria-label={`Riattiva l'avviso su ${a.label}`} disabled={patch.isPending} onClick={() => patch.mutate({ id: a.id, rearm: true })}>
            <RotateCcw className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button type="button" size="sm" variant="ghost" aria-label={`Elimina l'avviso su ${a.label}`} disabled={remove.isPending} onClick={() => remove.mutate(a.id)}>
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </span>
      {(patch.error || remove.error) && (
        <p role="alert" className="w-full text-loss">
          {errorMessage(patch.error ?? remove.error)}
        </p>
      )}
    </li>
  );
}

/**
 * «Avvisi di prezzo»: the owner's own price alerts — «sopra / sotto X» on a catalog
 * instrument or a «Mercati oggi» index — sent once to the Telegram group when hit (checked
 * every minute on cached prices). Separate from the agents' alerts: they never see these.
 */
export function PriceAlerts() {
  const q = usePriceAlerts();
  return (
    <div className="space-y-3">
      <AlertForm />
      {q.isError && <p className="text-sm text-loss">Impossibile caricare gli avvisi</p>}
      {q.data && q.data.length === 0 && <p className="text-sm text-muted">Nessun avviso</p>}
      {q.data && q.data.length > 0 && (
        <ul className="divide-y divide-border" aria-label="Avvisi di prezzo">
          {q.data.map((a) => (
            <AlertRow key={a.id} a={a} />
          ))}
        </ul>
      )}
      <p className="text-xs text-muted">Un avviso scatta una volta sola: messaggio su Telegram al gruppo, poi resta «Scattato» finché non lo riattivi o cambi la soglia.</p>
    </div>
  );
}
