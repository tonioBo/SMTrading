import { useState } from "react";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useInstrumentSearch } from "@/api/hooks/markets";
import { newKey, usePlaceOrder, usePreviewOrder } from "@/api/hooks/portfolios";
import type { Instrument, OrderInput, OrderPreview, OrderType, Portfolio, TimeInForce } from "@/api/types";
import { AmountPreview } from "@/components/AmountPreview";
import { FormField, selectClass } from "@/components/FormField";
import { LiveOrderConfirm } from "@/components/LiveOrderConfirm";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { AMBIGUOUS_AMOUNT_MESSAGE, amountError, multiplyDecimal, normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatMoney } from "@/lib/format";
import { ORDER_TYPE_LABELS, TIF_LABELS } from "@/lib/labels";
import { isTotpMissing } from "@/lib/live";

const ORDER_TYPES: OrderType[] = ["market", "limit", "stop", "stop_limit", "trailing_stop"];
const TIFS: TimeInForce[] = ["gtc", "day"];
const NOW_REAL_MESSAGE = "Il portafoglio ora è collegato al denaro reale: rifai l'anteprima";
const NEEDS_LIMIT: ReadonlySet<OrderType> = new Set(["limit", "stop_limit"]);
const NEEDS_STOP: ReadonlySet<OrderType> = new Set(["stop", "stop_limit"]);

type Field = "symbol" | "qty" | "notional" | "limit_price" | "stop_price" | "trail_pct" | "trail_amount";
type FieldErrors = Partial<Record<Field, string>>;
/** the fields with an input in the form: a server error about another one is shown above the buttons */
const FIELDS: ReadonlySet<string> = new Set<Field>(["symbol", "qty", "notional", "limit_price", "stop_price", "trail_pct", "trail_amount"]);

interface Draft {
  instrument: Instrument | null;
  side: "buy" | "sell";
  type: OrderType;
  /** «Controvalore» only for a market order */
  by: "qty" | "notional";
  amount: string;
  limitPrice: string;
  stopPrice: string;
  trailBy: "pct" | "amount";
  trail: string;
  tif: TimeInForce;
}

const EMPTY: Draft = {
  instrument: null,
  side: "buy",
  type: "market",
  by: "qty",
  amount: "",
  limitPrice: "",
  stopPrice: "",
  trailBy: "pct",
  trail: "",
  tif: "gtc",
};

/** A positive decimal for the API, else the message for its field. */
function positive(input: string): { value: string } | { error: string } {
  const value = normalizeAmount(input);
  if (value !== null && /[1-9]/.test(value)) return { value };
  return { error: amountError(input) === AMBIGUOUS_AMOUNT_MESSAGE ? AMBIGUOUS_AMOUNT_MESSAGE : "Inserisci un valore positivo" };
}

/** The request body of the draft, or the errors of its fields. */
function buildOrder(d: Draft): { body: OrderInput; errors: null } | { body: null; errors: FieldErrors } {
  const errors: FieldErrors = {};
  const body: OrderInput = { symbol: d.instrument?.symbol ?? "", side: d.side, type: d.type, tif: d.tif };
  if (!d.instrument) errors.symbol = "Scegli uno strumento dal catalogo";
  const set = (field: Field, input: string) => {
    const r = positive(input);
    if ("value" in r) (body as unknown as Record<string, string>)[field] = r.value;
    else errors[field] = r.error;
  };
  set(d.type === "market" && d.by === "notional" ? "notional" : "qty", d.amount);
  if (NEEDS_LIMIT.has(d.type)) set("limit_price", d.limitPrice);
  if (NEEDS_STOP.has(d.type)) set("stop_price", d.stopPrice);
  if (d.type === "trailing_stop") set(d.trailBy === "pct" ? "trail_pct" : "trail_amount", d.trail);
  return Object.keys(errors).length ? { body: null, errors } : { body, errors: null };
}

interface Previewed {
  json: string;
  body: OrderInput;
  result: OrderPreview;
  /** the Idempotency-Key of this previewed order, reused by its retries */
  key: string;
  /** the instrument's currency */
  currency: string;
}

/** The server asked for the TOTP (a code refused, or TOTP not enabled). */
const isTotpError = (err: unknown) => errorField(err) === "totp" || isTotpMissing(err);
/** A 4xx is the server's final word on this order: a retry needs a fresh preview (and key). */
const isDefinitive = (err: unknown) => err instanceof ApiError && err.status >= 400 && err.status < 500;

/** The estimated amount in the instrument currency: the notional as entered, else qty × price. */
function estimatedAmount(p: Previewed): { value: string; currency: string; eurEstimate: string | null } | null {
  const value = p.body.notional ?? (p.body.qty && p.result.estimated_price ? multiplyDecimal(p.body.qty, p.result.estimated_price) : null);
  return value ? { value, currency: p.currency, eurEstimate: p.result.estimated_notional_eur } : null;
}

/** The instrument picker: a search on the catalogue (GET /api/instruments), no instrument is created. */
function SymbolPicker({ selected, error, onPick }: { selected: Instrument | null; error?: string; onPick: (i: Instrument | null) => void }) {
  const [text, setText] = useState(selected?.symbol ?? "");
  const searching = !selected && text.trim().length > 0;
  const { data, isFetching } = useInstrumentSearch(searching ? text : "");
  return (
    <div className="space-y-1.5">
      <FormField label="Simbolo" error={error} hint={selected ? `${selected.name} · ${selected.market} · ${selected.currency}` : "Cerca per simbolo o nome"}>
        <Input
          autoComplete="off"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            if (selected) onPick(null);
          }}
        />
      </FormField>
      <div aria-live="polite">
        {searching && data && (
          <ul aria-label="Strumenti trovati" className="max-h-40 overflow-y-auto rounded-md border border-border text-sm">
            {data.items.length === 0 && !isFetching && <li className="px-3 py-1.5 text-muted">Nessuno strumento trovato</li>}
            {data.items.map((i) => (
              <li key={i.symbol}>
                <button
                  type="button"
                  className="flex w-full justify-between gap-4 px-3 py-1.5 text-left hover:bg-border/40"
                  onClick={() => {
                    setText(i.symbol);
                    onPick(i);
                  }}
                >
                  <span className="font-medium">{i.symbol}</span>
                  <span className="truncate text-muted">{i.name}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function PreviewSummary({ result, currency }: { result: OrderPreview; currency: string }) {
  if (!result.ok) {
    return (
      <p role="alert" className="text-sm text-loss">
        Ordine rifiutato: {result.message ?? result.code}
      </p>
    );
  }
  const rows: [string, string][] = [];
  if (result.estimated_price) rows.push(["Prezzo stimato", formatMoney(result.estimated_price, currency)]);
  if (result.estimated_notional_eur) rows.push(["Controvalore", formatMoney(result.estimated_notional_eur, "EUR")]);
  if (result.estimated_fee) rows.push(["Commissioni", formatMoney(result.estimated_fee.amount, result.estimated_fee.currency)]);
  if (result.estimated_fx_fee_eur && /[1-9]/.test(result.estimated_fx_fee_eur)) {
    rows.push(["Commissione di cambio", formatMoney(result.estimated_fx_fee_eur, "EUR")]);
  }
  if (result.buying_power_after_eur) rows.push(["Liquidità dopo l'ordine", formatMoney(result.buying_power_after_eur, "EUR")]);
  return (
    <dl aria-label="Anteprima dell'ordine" className="divide-y divide-border rounded-md border border-border text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4 px-3 py-1.5">
          <dt className="text-muted">{label}</dt>
          <dd className="num text-right">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * «Nuovo ordine» of a portfolio, mounted per opening. «Invia» is enabled only by an ok
 * «Anteprima» of the same fields: any change drops the preview. On a real-money portfolio
 * (the preview says `real`) «Invia» opens LiveOrderConfirm, which asks for the TOTP.
 */
export function OrderForm({ portfolio, onClose }: { portfolio: Portfolio; onClose: () => void }) {
  const preview = usePreviewOrder(portfolio.id);
  const place = usePlaceOrder(portfolio.id);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [previewed, setPreviewed] = useState<Previewed | null>(null);
  const [confirming, setConfirming] = useState(false);
  /** shown when a send without TOTP found the portfolio real */
  const [notice, setNotice] = useState<string | null>(null);

  const built = buildOrder(draft);
  const currentJson = built.body ? JSON.stringify(built.body) : null;
  const canSend = previewed !== null && previewed.result.ok && previewed.json === currentJson && !place.isPending;
  // a server error about a field (preview 400 or a refused placement) goes under that field
  const serverError = place.error ?? preview.error;
  const serverField = errorField(serverError);
  const fieldError = (f: Field) => errors[f] ?? (serverField === f ? errorMessage(serverError) : undefined);
  const generalError =
    notice ?? (serverError && !(serverField && FIELDS.has(serverField)) && !isTotpError(serverError) ? errorMessage(serverError) : null);

  function edit(patch: Partial<Draft>) {
    setDraft((d) => {
      const next = { ...d, ...patch };
      if (next.type !== "market") next.by = "qty";
      if (next.by !== d.by) next.amount = ""; // a controvalore is not a quantity
      return next;
    });
    setErrors({});
    setNotice(null);
    setPreviewed(null);
    preview.reset();
    place.reset();
  }

  function runPreview() {
    if (!built.body) {
      setErrors(built.errors);
      return;
    }
    const body = built.body;
    const json = JSON.stringify(body);
    const currency = draft.instrument?.currency ?? portfolio.base_currency;
    place.reset();
    setNotice(null);
    preview.mutate(body, { onSuccess: (result) => setPreviewed({ json, body, result, key: newKey(), currency }) });
  }

  function send(totp?: string) {
    if (!previewed) return;
    place.mutate(
      { body: totp ? { ...previewed.body, totp } : previewed.body, key: previewed.key },
      {
        onSuccess: () => {
          toast.success(`Ordine inviato: ${previewed.body.side === "buy" ? "acquisto" : "vendita"} ${previewed.body.symbol}`);
          onClose();
        },
        onError: (err) => {
          if (!totp && isTotpError(err)) {
            // linked to a real-money broker after the preview: preview again, then the TOTP
            setPreviewed(null);
            setNotice(NOW_REAL_MESSAGE);
          } else if (isDefinitive(err) && errorField(err) !== "totp") {
            // refused for good (the rejected order may be stored): a retry needs a fresh preview;
            // network errors and 5xx keep the preview and its key for a safe retry
            setPreviewed(null);
            setConfirming(false);
          }
        },
      },
    );
  }

  function close() {
    if (!place.isPending) onClose();
  }

  if (confirming && previewed) {
    const b = previewed.body;
    return (
      <LiveOrderConfirm
        portfolioName={portfolio.name}
        order={{
          symbol: b.symbol,
          side: b.side,
          type: b.type,
          qty: b.qty,
          notional: b.notional,
          limit_price: b.limit_price,
          stop_price: b.stop_price,
          trail_percent: b.trail_pct,
          trail_amount: b.trail_amount,
          tif: b.tif,
          currency: previewed.currency,
        }}
        amount={estimatedAmount(previewed)}
        pending={place.isPending}
        error={place.error}
        onClose={() => {
          if (place.isPending) return;
          place.reset();
          setConfirming(false);
        }}
        onConfirm={(totp) => send(totp)}
      />
    );
  }

  const ccy = draft.instrument?.currency;
  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Nuovo ordine</DialogTitle>
          <DialogDescription>Portafoglio {portfolio.name}</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            runPreview();
          }}
        >
          <SymbolPicker selected={draft.instrument} error={fieldError("symbol")} onPick={(instrument) => edit({ instrument })} />
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Lato">
              <select className={selectClass} value={draft.side} onChange={(e) => edit({ side: e.target.value as Draft["side"] })}>
                <option value="buy">Acquisto</option>
                <option value="sell">Vendita</option>
              </select>
            </FormField>
            <FormField label="Tipo">
              <select className={selectClass} value={draft.type} onChange={(e) => edit({ type: e.target.value as OrderType })}>
                {ORDER_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {ORDER_TYPE_LABELS[t]}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          {draft.type === "market" && (
            <FormField label="Ordine per">
              <select className={selectClass} value={draft.by} onChange={(e) => edit({ by: e.target.value as Draft["by"] })}>
                <option value="qty">Quantità</option>
                <option value="notional">Controvalore</option>
              </select>
            </FormField>
          )}
          {draft.by === "notional" ? (
            <FormField label="Controvalore" error={fieldError("notional")} hint={<AmountPreview input={draft.amount} currency={ccy} />}>
              <Input inputMode="decimal" value={draft.amount} onChange={(e) => edit({ amount: e.target.value })} />
            </FormField>
          ) : (
            <FormField label="Quantità" error={fieldError("qty")} hint={<AmountPreview input={draft.amount} />}>
              <Input inputMode="decimal" value={draft.amount} onChange={(e) => edit({ amount: e.target.value })} />
            </FormField>
          )}
          {NEEDS_STOP.has(draft.type) && (
            <FormField label="Prezzo stop" error={fieldError("stop_price")} hint={<AmountPreview input={draft.stopPrice} currency={ccy} />}>
              <Input inputMode="decimal" value={draft.stopPrice} onChange={(e) => edit({ stopPrice: e.target.value })} />
            </FormField>
          )}
          {NEEDS_LIMIT.has(draft.type) && (
            <FormField label="Prezzo limite" error={fieldError("limit_price")} hint={<AmountPreview input={draft.limitPrice} currency={ccy} />}>
              <Input inputMode="decimal" value={draft.limitPrice} onChange={(e) => edit({ limitPrice: e.target.value })} />
            </FormField>
          )}
          {draft.type === "trailing_stop" && (
            <div className="grid grid-cols-2 gap-3">
              <FormField label="Trailing in">
                <select className={selectClass} value={draft.trailBy} onChange={(e) => edit({ trailBy: e.target.value as Draft["trailBy"] })}>
                  <option value="pct">Percentuale</option>
                  <option value="amount">Importo</option>
                </select>
              </FormField>
              <FormField
                label="Distanza trailing"
                error={fieldError(draft.trailBy === "pct" ? "trail_pct" : "trail_amount")}
                hint={draft.trailBy === "pct" ? <AmountPreview input={draft.trail} suffix="%" /> : <AmountPreview input={draft.trail} currency={ccy} />}
              >
                <Input inputMode="decimal" value={draft.trail} onChange={(e) => edit({ trail: e.target.value })} />
              </FormField>
            </div>
          )}
          <FormField label="Durata">
            <select className={selectClass} value={draft.tif} onChange={(e) => edit({ tif: e.target.value as TimeInForce })}>
              {TIFS.map((t) => (
                <option key={t} value={t}>
                  {TIF_LABELS[t]}
                </option>
              ))}
            </select>
          </FormField>
          {previewed && <PreviewSummary result={previewed.result} currency={previewed.currency} />}
          {generalError && (
            <p role="alert" className="text-sm text-loss">
              {generalError}
            </p>
          )}
          {!canSend && !place.isPending && (
            <p id="order-send-hint" className="text-right text-xs text-muted">
              Fai prima l'anteprima
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close} disabled={place.isPending}>
              Annulla
            </Button>
            <Button type="submit" variant="outline" disabled={preview.isPending || place.isPending}>
              Anteprima
            </Button>
            <Button
              type="button"
              disabled={!canSend}
              aria-describedby={canSend || place.isPending ? undefined : "order-send-hint"}
              onClick={() => (previewed?.result.real ? setConfirming(true) : send())}
            >
              Invia
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
