import { formatMoney } from "@/lib/format";

/** An amount in `currency` (default EUR), it-IT formatted; null shows an em dash. */
export function Money({ value, currency = "EUR" }: { value: string | null | undefined; currency?: string }) {
  return <span className="num">{value == null ? "—" : formatMoney(value, currency)}</span>;
}
