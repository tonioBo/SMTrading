import { useIntradayAccounts } from "@/api/hooks/intraday";
import type { IntradayAccount, IntradayAccountPortfolio } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatMoney } from "@/lib/format";

const inDay = (p: IntradayAccountPortfolio) => p.intraday_enabled || p.auto_rules_enabled;

/** «gg/mm» of a yyyy-mm-dd date. */
const dayMonth = (iso: string) => (/^\d{4}-\d{2}-\d{2}/.test(iso) ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}` : iso);

/** «potere d'acquisto 25.000,00 $ · margine ×2 · letto alle 10:30» (broker account state). */
function accountLine(a: IntradayAccount): string {
  const parts = [a.buying_power == null ? "potere d'acquisto non noto" : `potere d'acquisto ${formatMoney(a.buying_power, "USD")}`];
  if (a.multiplier != null) parts.push(`margine ×${Number(a.multiplier)}`);
  if (a.read_at == null) parts.push("non letto");
  else parts.push(a.fresh ? `letto alle ${formatDateTime(a.read_at).slice(11, 16)}` : "non letto di recente");
  return parts.join(" · ");
}

/** The cap usage of a portfolio in day mode. */
function capLine(p: IntradayAccountPortfolio): string {
  const used = p.used ?? 0;
  if (p.max_day_trades_5d == null) return `usate ${used} (nessun tetto)`;
  const left = `usate ${used} su ${p.max_day_trades_5d} · restano ${p.remaining ?? 0}`;
  return p.frees_on ? `${left} · la prossima si libera il ${dayMonth(p.frees_on)}` : left;
}

function AccountBox({ a }: { a: IntradayAccount }) {
  const rows = a.portfolios.filter(inDay);
  const blocked = a.trading_blocked === true || a.account_blocked === true;
  return (
    <section aria-label={`Conto e operazioni di giornata ${a.link_name}`} className="space-y-2 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">{a.link_name}</span>
        {blocked && <StatusBadge tone="danger">bloccato dal broker</StatusBadge>}
      </div>
      <p className="num">{accountLine(a)}</p>
      <p className="text-muted">{a.margin ? `leva disponibile ${Number(a.multiplier)}×` : "senza leva"}</p>
      <ul className="space-y-1">
        {rows.map((p) => (
          <li key={p.portfolio_id} className="flex flex-wrap gap-x-3">
            <span className="font-medium">{p.portfolio_name}</span>
            <span className="num text-muted">{`leva in giornata ${Number(p.day_leverage)}`}</span>
            <span className="num text-muted">{capLine(p)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** «Conto e operazioni di giornata»: one box per broker link with a portfolio in day mode. */
export function DayAccountBoxes() {
  const { data } = useIntradayAccounts();
  const links = (data ?? []).filter((a) => a.portfolios.some(inDay));
  if (links.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Conto e operazioni di giornata</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {links.map((a) => (
          <AccountBox key={a.link_id} a={a} />
        ))}
      </CardContent>
    </Card>
  );
}
