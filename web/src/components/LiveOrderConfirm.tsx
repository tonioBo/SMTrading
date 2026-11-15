import { useState } from "react";
import { ApiError } from "@/api/client";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { TotpErrors, TotpField } from "@/components/LiveTotpDialog";
import { formatMoney, formatQty } from "@/lib/format";
import { TOTP_RE, TRAILING_NOTE } from "@/lib/live";
import { ORDER_TYPE_LABELS, TIF_LABELS } from "@/lib/labels";

export interface LiveOrderSummary {
  symbol: string;
  side: "buy" | "sell";
  type: string;
  qty?: string | null;
  notional?: string | null;
  limit_price?: string | null;
  stop_price?: string | null;
  trail_percent?: string | null;
  trail_amount?: string | null;
  tif?: string | null;
  /** the instrument's currency: notional and prices are shown in it */
  currency?: string | null;
}

interface Props {
  portfolioName: string;
  order: LiveOrderSummary;
  /** the estimated amount in the instrument currency, with the preview's EUR estimate */
  amount: { value: string; currency: string; eurEstimate?: string | null } | null;
  pending?: boolean;
  /** the placement error: one about the TOTP (`field=totp`) is shown under the code; any
   * answer of the server clears the code (it may have been spent), a network error keeps it */
  error?: unknown;
  onClose: () => void;
  onConfirm: (totp: string) => void;
}

/**
 * An admin's manual order on a real-money portfolio: «🔴 Ordine con denaro reale» with the
 * summary, the estimated amount and the TOTP code. Any server answer clears the field for the
 * next one. A trailing stop is simulated by SMTrading (the worker must be running), never
 * placed at the broker: the dialog says so.
 */
export function LiveOrderConfirm({ portfolioName, order, amount, pending = false, error = null, onClose, onConfirm }: Props) {
  const [totp, setTotp] = useState("");
  const [seenError, setSeenError] = useState<unknown>(error);
  if (error !== seenError) {
    setSeenError(error);
    if (error && !(error instanceof ApiError && error.status === 0)) setTotp(""); // single-use: ask for the next code
  }

  const rows: [string, string][] = [
    ["Portafoglio", portfolioName],
    ["Simbolo", order.symbol],
    ["Lato", order.side === "buy" ? "Acquisto" : "Vendita"],
    ["Tipo", ORDER_TYPE_LABELS[order.type] ?? order.type],
  ];
  const money = (v: string) => (order.currency ? formatMoney(v, order.currency) : formatQty(v));
  // prices stay exact (no rounding to cents: a crypto limit may have more decimals)
  const price = (v: string) => (order.currency ? `${formatQty(v)} ${order.currency}` : formatQty(v));
  if (order.qty) rows.push(["Quantità", formatQty(order.qty)]);
  if (order.notional) rows.push(["Controvalore", money(order.notional)]);
  if (order.limit_price) rows.push(["Prezzo limite", price(order.limit_price)]);
  if (order.stop_price) rows.push(["Prezzo stop", price(order.stop_price)]);
  if (order.trail_percent) rows.push(["Trailing", `${formatQty(order.trail_percent)}%`]);
  if (order.trail_amount) rows.push(["Trailing", price(order.trail_amount)]);
  if (order.tif) rows.push(["Durata", TIF_LABELS[order.tif] ?? order.tif]);
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="🔴 Ordine con denaro reale"
      description="L'ordine va al conto reale del broker collegato."
      confirmLabel="Invia ordine reale"
      destructive
      pending={pending}
      confirmDisabled={!TOTP_RE.test(totp)}
      onConfirm={() => onConfirm(totp)}
    >
      <dl aria-label="Riepilogo dell'ordine" className="divide-y divide-border rounded-md border border-loss/40 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 px-3 py-1.5">
            <dt className="text-muted">{label}</dt>
            <dd className="num text-right">{value}</dd>
          </div>
        ))}
        {amount && (
          <div className="flex justify-between gap-4 px-3 py-1.5">
            <dt className="text-muted">Importo stimato</dt>
            <dd className="num text-right">
              <span className="font-semibold">{formatMoney(amount.value, amount.currency)}</span>
              {amount.eurEstimate && amount.currency !== "EUR" && (
                <span className="block text-xs text-muted">≈ {formatMoney(amount.eurEstimate, "EUR")} (stima)</span>
              )}
            </dd>
          </div>
        )}
      </dl>
      {order.type === "trailing_stop" && (
        <p role="note" className="text-sm font-medium text-warn">
          Trailing stop: {TRAILING_NOTE}.
        </p>
      )}
      <TotpField value={totp} onChange={setTotp} error={error} />
      <TotpErrors error={error} />
    </ConfirmDialog>
  );
}
