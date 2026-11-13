import { AMBIGUOUS_AMOUNT_MESSAGE, parseAmount } from "@/lib/decimal";
import { formatMoney, formatQty } from "@/lib/format";

/**
 * The amount as it will be sent, it-IT formatted (money with `currency`, else a quantity
 * with an optional `suffix` such as "%"), or the ambiguity warning for "10.000"-like input.
 */
export function AmountPreview({ input, currency, suffix = "" }: { input: string; currency?: string; suffix?: string }) {
  if (!input.trim()) return null;
  const parsed = parseAmount(input);
  if (!parsed.ok) {
    return parsed.error === AMBIGUOUS_AMOUNT_MESSAGE ? (
      <span className="text-warn" data-testid="amount-preview">
        {parsed.error}
      </span>
    ) : null;
  }
  const text = currency ? formatMoney(parsed.value, currency.trim().toUpperCase() || "EUR") : `${formatQty(parsed.value)}${suffix}`;
  return (
    <span className="num" data-testid="amount-preview">
      = {text}
    </span>
  );
}
