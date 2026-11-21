import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { useFacets } from "@/api/hooks/marketsExplore";
import { useMarketNews } from "@/api/hooks/marketsV2";
import { selectClass } from "@/components/FormField";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { formatClock } from "@/lib/format";

/**
 * «Notizie»: the day's market headlines (broad index ETFs) or, with a filter, those of one
 * instrument, of a sector's main names, or of what the portfolios hold — through the
 * market data router and its relevance check, cached 10 minutes on the server.
 */
export function MarketNews({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [symbol, setSymbol] = useState("");
  const [typed, setTyped] = useState("");
  const [sector, setSector] = useState("");
  const [portfolios, setPortfolios] = useState(false);
  const facets = useFacets();
  const q = useMarketNews({ symbol, sector, portfolios });
  const d = q.data;
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3 sm:items-center">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSymbol(typed.trim().toUpperCase());
          }}
        >
          <Input aria-label="Notizie di un titolo" placeholder="Titolo (es. AAPL), Invio" value={typed} onChange={(e) => setTyped(e.target.value)} onBlur={() => setSymbol(typed.trim().toUpperCase())} />
        </form>
        <select aria-label="Notizie di un settore" className={selectClass} value={sector} disabled={!!symbol || portfolios} onChange={(e) => setSector(e.target.value)}>
          <option value="">Tutti i settori</option>
          {(facets.data?.sectors ?? []).map((s) => (
            <option key={s.sector} value={s.sector}>
              {s.sector}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm">
          <Switch aria-label="Solo portafogli" checked={portfolios} disabled={!!symbol} onCheckedChange={setPortfolios} />
          Solo portafogli
        </label>
      </div>
      {q.isError && <p className="text-sm text-loss">{symbol ? `Titolo ${symbol} non trovato` : "Notizie non disponibili"}</p>}
      {q.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {d && d.error && <p className="text-sm text-muted">Notizie non disponibili al momento</p>}
      {d && !d.error && d.items.length === 0 && <p className="text-sm text-muted">Nessuna notizia recente</p>}
      {d && d.items.length > 0 && (
        <ul className="space-y-2.5" aria-label="Notizie">
          {d.items.map((n) => (
            <li key={n.id} className="text-sm">
              {n.url ? (
                <a href={n.url} target="_blank" rel="noreferrer noopener" className="inline-flex items-start gap-1 font-medium hover:underline">
                  {n.headline}
                  <ExternalLink className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
                </a>
              ) : (
                <span className="font-medium">{n.headline}</span>
              )}
              <span className="flex flex-wrap gap-x-2 text-xs text-muted">
                <span>
                  {formatClock(n.ts)} · {n.source}
                </span>
                {n.symbols.map((s) => (
                  <button key={s} type="button" className="num hover:underline" onClick={() => onOpen(s)}>
                    {s}
                  </button>
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
      {d && d.symbols.length > 0 && <p className="text-xs text-muted">Da: {d.symbols.join(", ")}</p>}
    </div>
  );
}
