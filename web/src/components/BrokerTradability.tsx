import { useState } from "react";
import { useTradability } from "@/api/hooks/portfolios";
import type { TradabilityItem } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

const MAX_SYMBOLS = 50;

/** «A, b ,c» → ["A", "B", "C"] (no duplicates, at most 50). */
function parseSymbols(text: string): string[] {
  const out = text
    .split(/[\s,;]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  return [...new Set(out)].slice(0, MAX_SYMBOLS);
}

function yesNo(v: boolean | null): string {
  return v == null ? "—" : v ? "sì" : "no";
}

function Status({ item }: { item: TradabilityItem }) {
  if (item.tradable == null) return <Badge variant="secondary">Non verificato</Badge>;
  return item.tradable ? <Badge>Acquistabile</Badge> : <Badge variant="destructive">Non acquistabile</Badge>;
}

/** «Strumenti presso il broker» (plan 44): which instruments the broker of a linked
 * portfolio lets SMTrading buy (without leverage), typed or from the strategy universes. */
export function BrokerTradability({ portfolioId }: { portfolioId: string }) {
  const [text, setText] = useState("");
  const [symbols, setSymbols] = useState<string[]>([]);
  const [refresh, setRefresh] = useState(false);
  const check = useTradability(portfolioId, symbols, refresh);
  const universes = check.data?.universes ?? [];

  const run = (list: string[], again = false) => {
    setRefresh(again);
    setSymbols(list);
  };

  return (
    <Card role="region" aria-label="Strumenti presso il broker">
      <CardHeader>
        <CardTitle>Strumenti presso il broker</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted">
          Quali strumenti il broker collegato lascia comprare senza leva. La risposta del broker vale 12 ore; gli acquisti
          di uno strumento non acquistabile sono rifiutati subito.
        </p>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            run(parseSymbols(text));
          }}
        >
          <label htmlFor="tradability-symbols" className="sr-only">
            Simboli da verificare
          </label>
          <Input
            id="tradability-symbols"
            className="w-72"
            placeholder="Simboli, es. SPY, EUNL.DE"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <Button type="submit" size="sm" disabled={parseSymbols(text).length === 0}>
            Verifica
          </Button>
          {symbols.length > 0 && (
            <Button type="button" size="sm" variant="outline" onClick={() => run(symbols, true)} disabled={check.isFetching}>
              Chiedi di nuovo al broker
            </Button>
          )}
        </form>
        {universes.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted">Universi delle strategie:</span>
            {universes.map((u) => (
              <Button
                key={u.ref}
                type="button"
                size="sm"
                variant="outline"
                title={u.symbols.join(", ")}
                onClick={() => {
                  setText(u.symbols.join(", "));
                  run(u.symbols.slice(0, MAX_SYMBOLS));
                }}
              >
                {u.name ? `${u.ref} · ${u.name}` : u.ref}
              </Button>
            ))}
          </div>
        )}
        {check.isError && <p className="text-loss">{errorMessage(check.error, "Verifica non riuscita")}</p>}
        {symbols.length > 0 && check.isFetching && <p className="text-muted">Verifica in corso…</p>}
        {symbols.length > 0 && check.data && !check.isFetching && (
          <table className="w-full text-left" aria-label="Strumenti presso il broker">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-medium">Simbolo</th>
                <th className="py-1 pr-3 font-medium">Stato</th>
                <th className="py-1 pr-3 font-medium">Short</th>
                <th className="py-1 pr-3 font-medium">Frazioni</th>
                <th className="py-1 font-medium">Motivo</th>
              </tr>
            </thead>
            <tbody>
              {check.data.items.map((item) => (
                <tr key={item.symbol} className="border-t border-border">
                  <td className="py-1 pr-3 font-medium">{item.symbol}</td>
                  <td className="py-1 pr-3">
                    <Status item={item} />
                  </td>
                  <td className="py-1 pr-3">{yesNo(item.short)}</td>
                  <td className="py-1 pr-3">{yesNo(item.fractional)}</td>
                  <td className="py-1 text-muted">{item.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}
