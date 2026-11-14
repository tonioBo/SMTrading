import { ConfirmDialog } from "@/components/ConfirmDialog";
import { formatQty } from "@/lib/format";
import { TRAILING_NOTE } from "@/lib/live";

export interface LiveOrderSummary {
  symbol: string;
  side: "buy" | "sell";
  type: string;
  qty?: string | null;
  notional?: string | null;
  limit_price?: string | null;
  stop_price?: string | null;
  trail_percent?: string | null;
  tif?: string | null;
}

const TYPE_LABELS: Record<string, string> = {
  market: "A mercato",
  limit: "Limite",
  stop: "Stop",
  stop_limit: "Stop limite",
  trailing_stop: "Trailing stop",
};

interface Props {
  portfolioName: string;
  order: LiveOrderSummary;
  pending?: boolean;
  error?: string | null;
  onClose: () => void;
  onConfirm: () => void;
}

/**
 * An admin's manual order on a real-money portfolio: «Ordine con denaro reale» with the
 * summary, one click, no TOTP. A trailing stop is simulated by SMTrading (the worker must
 * be running), never placed at the broker: the dialog says so.
 */
export function LiveOrderConfirm({ portfolioName, order, pending = false, error = null, onClose, onConfirm }: Props) {
  const rows: [string, string][] = [
    ["Portafoglio", portfolioName],
    ["Simbolo", order.symbol],
    ["Lato", order.side === "buy" ? "Acquisto" : "Vendita"],
    ["Tipo", TYPE_LABELS[order.type] ?? order.type],
  ];
  if (order.qty) rows.push(["Quantità", formatQty(order.qty)]);
  if (order.notional) rows.push(["Controvalore", formatQty(order.notional)]);
  if (order.limit_price) rows.push(["Prezzo limite", formatQty(order.limit_price)]);
  if (order.stop_price) rows.push(["Prezzo stop", formatQty(order.stop_price)]);
  if (order.trail_percent) rows.push(["Trailing", `${formatQty(order.trail_percent)}%`]);
  if (order.tif) rows.push(["Validità", order.tif]);
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Ordine con denaro reale"
      description="L'ordine va al conto reale del broker collegato."
      confirmLabel="Invia ordine reale"
      destructive
      pending={pending}
      error={error}
      onConfirm={onConfirm}
    >
      <dl aria-label="Riepilogo dell'ordine" className="divide-y divide-border rounded-md border border-loss/40 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 px-3 py-1.5">
            <dt className="text-muted">{label}</dt>
            <dd className="num text-right">{value}</dd>
          </div>
        ))}
      </dl>
      {order.type === "trailing_stop" && (
        <p role="note" className="text-sm font-medium text-warn">
          Trailing stop: {TRAILING_NOTE}.
        </p>
      )}
    </ConfirmDialog>
  );
}
