import { ArrowDown, ArrowUp } from "lucide-react";
import { useMemo } from "react";
import { usePageDayChanges, usePageQuotes } from "@/api/hooks/marketsExplore";
import { useFavorites, useReorderFavorites } from "@/api/hooks/marketsV2";
import { FavoriteStar } from "@/components/markets/FavoriteStar";
import { Pct } from "@/components/Pct";
import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";
import { errorMessage } from "@/lib/errors";

/**
 * «I miei preferiti»: the signed-in user's starred instruments with live prices (one batch
 * quote and one batch day change request for them, refreshed every minute), in their own
 * order (↑ ↓). Personal: apart from the agents' watchlists.
 */
export function Favorites({ onOpen }: { onOpen: (symbol: string) => void }) {
  const favs = useFavorites();
  const reorder = useReorderFavorites();
  const items = useMemo(() => favs.data?.items ?? [], [favs.data]);
  const symbols = useMemo(() => items.slice(0, 50).map((f) => f.symbol), [items]);
  const quotes = usePageQuotes(symbols);
  const changes = usePageDayChanges(symbols);
  const price = (sym: string): string | null => {
    const q = quotes.data?.[sym];
    if (q && !("error" in q) && q.last) return q.last;
    return changes.data?.[sym]?.last ?? null;
  };
  const move = (i: number, by: number) => {
    const order = items.map((f) => f.symbol);
    const [s] = order.splice(i, 1);
    order.splice(i + by, 0, s);
    reorder.mutate(order);
  };
  if (favs.isLoading) return <p className="text-sm text-muted">Caricamento…</p>;
  if (favs.isError) return <p className="text-sm text-loss">Impossibile caricare i preferiti</p>;
  if (items.length === 0)
    return <p className="text-sm text-muted">Nessun preferito. Tocca la stella ☆ accanto a uno strumento (elenco, ricerca o scheda) per seguirlo qui.</p>;
  return (
    <div className="space-y-2">
      <ul className="divide-y divide-border" aria-label="I miei preferiti">
        {items.map((f, i) => (
          <li key={f.symbol} className="flex items-center gap-2 py-1.5">
            <FavoriteStar symbol={f.symbol} />
            <button type="button" className="flex min-w-0 flex-1 items-baseline gap-2 text-left hover:underline" onClick={() => onOpen(f.symbol)} aria-label={`Scheda ${f.symbol}`}>
              <span className="num w-20 shrink-0 font-medium">{f.symbol}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-muted">{f.name}</span>
            </button>
            <span className="num w-28 shrink-0 text-right text-sm">{price(f.symbol) ? formatMoney(price(f.symbol)!, f.currency) : "—"}</span>
            <Pct value={changes.data?.[f.symbol]?.change_pct ?? null} tone className="w-16 shrink-0 text-right text-sm" />
            <span className="hidden shrink-0 gap-0.5 sm:flex">
              <Button type="button" size="sm" variant="ghost" aria-label={`Sposta ${f.symbol} su`} disabled={i === 0 || reorder.isPending} onClick={() => move(i, -1)}>
                <ArrowUp className="h-3.5 w-3.5" />
              </Button>
              <Button type="button" size="sm" variant="ghost" aria-label={`Sposta ${f.symbol} giù`} disabled={i === items.length - 1 || reorder.isPending} onClick={() => move(i, 1)}>
                <ArrowDown className="h-3.5 w-3.5" />
              </Button>
            </span>
          </li>
        ))}
      </ul>
      {reorder.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(reorder.error)}
        </p>
      )}
      <p className="text-xs text-muted">Prezzi aggiornati ogni minuto; variazione rispetto alla chiusura precedente.</p>
    </div>
  );
}
