import { useFundamentals } from "@/api/hooks/marketsV2";
import { formatDecimal, formatMoney } from "@/lib/format";
import { bigNumber } from "@/lib/marketsV2";

const ND = "non disponibile";
const fmtDay = (d: string) => d.split("-").reverse().join("/");

/**
 * «Dati fondamentali» of the instrument sheet: market cap, P/E (trailing and forward),
 * dividend yield, 52-week range, beta and the next earnings date (Yahoo, cached 12 h on
 * the server); «non disponibile» for whatever the source does not have.
 */
export function FundamentalsSection({ symbol, currency }: { symbol: string; currency: string }) {
  const q = useFundamentals(symbol);
  const f = q.data?.symbol === symbol ? q.data : undefined;
  if (q.isLoading) return <div className="h-20 animate-pulse rounded bg-card" />;
  if (!f) return <p className="text-sm text-muted">Dati fondamentali non disponibili</p>;
  const cur = f.currency ?? currency;
  const range = f.low_52w && f.high_52w ? `${formatMoney(f.low_52w, cur)} – ${formatMoney(f.high_52w, cur)}` : ND;
  const cells: [string, string, string | null][] = [
    ["Capitalizzazione", f.market_cap ? `${bigNumber(f.market_cap)} ${cur}` : ND, null],
    ["P/E (ultimi 12 mesi)", f.pe_trailing ? formatDecimal(f.pe_trailing, 1) : ND, null],
    ["P/E (stimato)", f.pe_forward ? formatDecimal(f.pe_forward, 1) : ND, null],
    ["Rendimento dividendo", f.dividend_yield ? `${formatDecimal(f.dividend_yield, 2)}%` : ND, f.ex_dividend ? `stacco ${fmtDay(f.ex_dividend)}` : null],
    ["Minimo – massimo 52 settimane", range, null],
    ["Beta", f.beta ? formatDecimal(f.beta, 2) : ND, null],
    ["Prossima trimestrale", f.next_earnings ? fmtDay(f.next_earnings) : ND, f.next_earnings && f.earnings_estimate ? "data stimata" : null],
  ];
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      {cells.map(([k, v, note]) => (
        <div key={k} className="rounded-md border border-border p-2">
          <dt className="text-xs text-muted">{k}</dt>
          <dd className={v === ND ? "text-sm text-muted" : "num text-sm font-medium"}>{v}</dd>
          {note && <dd className="text-xs text-muted">{note}</dd>}
        </div>
      ))}
    </dl>
  );
}
