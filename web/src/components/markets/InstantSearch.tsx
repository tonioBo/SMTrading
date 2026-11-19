import { Search } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useRankedSearch } from "@/api/hooks/marketsExplore";
import type { SearchHit } from "@/api/marketsTypes";
import type { AssetClass } from "@/api/types";
import { Input } from "@/components/ui/input";
import { ASSET_CLASS_LABELS } from "@/lib/labels";
import { highlightParts } from "@/lib/marketsPrefs";
import { INDEX_LABELS } from "@/components/markets/labels";

export const SEARCH_DEBOUNCE_MS = 150;
const CLASS_ORDER: AssetClass[] = ["us_equity", "eu_equity", "crypto", "forex"];

function Highlight({ text, query }: { text: string; query: string }) {
  return (
    <>
      {highlightParts(text, query).map((p, i) =>
        p.hit ? (
          <mark key={i} className="rounded-sm bg-accent/25 text-fg">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}

/** The detail line of a hit: what matched when it was not the symbol or the name. */
function detail(h: SearchHit): string {
  const idx = (h.indices ?? []).map((c) => INDEX_LABELS[c] ?? c).join(", ");
  const parts = [h.market, h.sector ?? null, h.match === "industry" ? h.industry : null, h.match === "index" ? idx : null];
  return parts.filter(Boolean).join(" · ");
}

/**
 * Instant search across the whole catalog: debounced ~150 ms, results grouped by asset
 * class with the matching text highlighted; ↑ ↓ move, Enter opens the sheet, Esc closes.
 */
export function InstantSearch({ onOpen }: { onOpen: (symbol: string) => void }) {
  const [text, setText] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const listId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(text.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [text]);
  const { data, isFetching } = useRankedSearch(debounced);
  const hits = useMemo(() => (debounced ? (data?.items ?? []) : []), [data, debounced]);

  // grouped by asset class (the group of the best hit first), flattened for the keyboard
  const groups = useMemo(() => {
    const by = new Map<AssetClass, SearchHit[]>();
    for (const h of hits) by.set(h.asset_class, [...(by.get(h.asset_class) ?? []), h]);
    return [...by.entries()].sort((a, b) => hits.indexOf(a[1][0]) - hits.indexOf(b[1][0]) || CLASS_ORDER.indexOf(a[0]) - CLASS_ORDER.indexOf(b[0]));
  }, [hits]);
  const flat = useMemo(() => groups.flatMap(([, hs]) => hs), [groups]);
  useEffect(() => setActive(0), [debounced]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  function choose(h: SearchHit | undefined) {
    if (!h) return;
    setOpen(false);
    onOpen(h.symbol);
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, Math.max(flat.length - 1, 0)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(flat[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const showList = open && debounced.length > 0;
  const optionId = (i: number) => `${listId}-opt-${i}`;
  const position = new Map(flat.map((h, i) => [h.symbol, i]));
  return (
    <div ref={boxRef} className="relative w-full">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden="true" />
      <Input
        role="combobox"
        aria-label="Ricerca istantanea"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList && flat[active] ? optionId(active) : undefined}
        placeholder="Cerca simbolo, nome, settore o indice (es. «nvda», «banche», «dax»)"
        className="pl-9"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
      />
      {showList && (
        <div id={listId} role="listbox" aria-label="Risultati della ricerca" className="absolute z-40 mt-1 max-h-[70vh] w-full overflow-y-auto rounded-md border border-border bg-bg p-1 shadow-lg">
          {flat.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted">{isFetching || debounced !== text.trim() ? "Ricerca…" : "Nessuno strumento trovato"}</p>
          ) : (
            groups.map(([ac, hs]) => (
              <div key={ac} role="group" aria-label={ASSET_CLASS_LABELS[ac] ?? ac}>
                <div className="px-3 pb-1 pt-2 text-xs font-medium uppercase tracking-wide text-muted">{ASSET_CLASS_LABELS[ac] ?? ac}</div>
                {hs.map((h) => {
                  const i = position.get(h.symbol) ?? 0;
                  return (
                    <div
                      key={h.symbol}
                      id={optionId(i)}
                      role="option"
                      aria-selected={i === active}
                      className={`flex cursor-pointer items-baseline gap-3 rounded px-3 py-2 text-sm ${i === active ? "bg-card" : ""}`}
                      onMouseEnter={() => setActive(i)}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => choose(h)}
                    >
                      <span className="num w-24 shrink-0 font-medium">
                        <Highlight text={h.symbol} query={debounced} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">
                          <Highlight text={h.name} query={debounced} />
                        </span>
                        <span className="block truncate text-xs text-muted">
                          <Highlight text={detail(h)} query={h.match === "sector" || h.match === "industry" ? debounced : ""} />
                        </span>
                      </span>
                      {!h.active && <span className="text-xs text-muted">disattivato</span>}
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
