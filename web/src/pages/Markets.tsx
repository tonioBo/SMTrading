import { Plus } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useStreamStatus } from "@/api/hooks/connectors";
import { useCreateInstrument, useInstruments, useMarketsStatus, useQuotes, useTrack } from "@/api/hooks/markets";
import type { AssetClass, Instrument, InstrumentCreate, MarketStatus, Quote } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime, formatQty } from "@/lib/format";
import { useLiveMessage, useLiveSocket } from "@/lib/ws";

const ASSET_CLASSES: AssetClass[] = ["us_equity", "eu_equity", "crypto", "forex"];
const SYMBOL_RE = /^[A-Z0-9][A-Z0-9.\-_/=^]{0,63}$/;
const MAX_GRID_SYMBOLS = 50;

const isPositive = (v: string | null) => v !== null && /[1-9]/.test(v);

// --- add to catalog ------------------------------------------------------------------

function AddInstrumentDialog({ markets, onClose }: { markets: string[]; onClose: () => void }) {
  const create = useCreateInstrument();
  const [form, setForm] = useState({
    symbol: "",
    name: "",
    asset_class: "us_equity" as AssetClass,
    market: markets[0] ?? "",
    currency: "USD",
    lot_size: "1",
    tick_size: "0,01",
    shortable: false,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  const serverField = errorField(create.error);
  const fieldError = (f: string) => errors[f] ?? (create.error && serverField === f ? errorMessage(create.error) : null);
  const general = create.error && (!serverField || !(serverField in form)) ? errorMessage(create.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    const symbol = form.symbol.trim().toUpperCase();
    if (!SYMBOL_RE.test(symbol)) errs.symbol = "Simbolo non valido";
    if (!form.name.trim() || form.name.trim().length > 256) errs.name = "Il nome è obbligatorio (max 256 caratteri)";
    if (!markets.includes(form.market)) errs.market = "Seleziona un mercato";
    if (!/^[A-Za-z]{3}$/.test(form.currency.trim())) errs.currency = "La valuta deve avere 3 lettere";
    const lot = normalizeAmount(form.lot_size);
    const tick = normalizeAmount(form.tick_size);
    if (!isPositive(lot)) errs.lot_size = "Deve essere maggiore di zero";
    if (!isPositive(tick)) errs.tick_size = "Deve essere maggiore di zero";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const body: InstrumentCreate = {
      symbol,
      name: form.name.trim(),
      asset_class: form.asset_class,
      market: form.market,
      currency: form.currency.trim().toUpperCase(),
      lot_size: lot!,
      tick_size: tick!,
      shortable: form.shortable,
    };
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Aggiungi strumento</DialogTitle>
          <DialogDescription>Aggiunge uno strumento all&apos;anagrafica.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate className="grid gap-3 sm:grid-cols-2">
          <FormField label="Simbolo" error={fieldError("symbol")}>
            <Input value={form.symbol} onChange={(e) => set({ symbol: e.target.value })} autoFocus />
          </FormField>
          <FormField label="Nome" error={fieldError("name")}>
            <Input value={form.name} onChange={(e) => set({ name: e.target.value })} />
          </FormField>
          <FormField label="Classe di asset" error={fieldError("asset_class")}>
            <select className={selectClass} value={form.asset_class} onChange={(e) => set({ asset_class: e.target.value as AssetClass })}>
              {ASSET_CLASSES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Mercato" error={fieldError("market")}>
            <select className={selectClass} value={form.market} onChange={(e) => set({ market: e.target.value })}>
              {markets.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Valuta" error={fieldError("currency")}>
            <Input value={form.currency} maxLength={3} onChange={(e) => set({ currency: e.target.value })} />
          </FormField>
          <FormField label="Lotto minimo" error={fieldError("lot_size")}>
            <Input value={form.lot_size} inputMode="decimal" onChange={(e) => set({ lot_size: e.target.value })} />
          </FormField>
          <FormField label="Tick size" error={fieldError("tick_size")}>
            <Input value={form.tick_size} inputMode="decimal" onChange={(e) => set({ tick_size: e.target.value })} />
          </FormField>
          <div className="flex items-center gap-2 self-end pb-2">
            <input id="inst-short" type="checkbox" checked={form.shortable} onChange={(e) => set({ shortable: e.target.checked })} className="h-4 w-4 accent-[rgb(var(--accent))]" />
            <label htmlFor="inst-short">Vendibile allo scoperto</label>
          </div>
          {general && (
            <p role="alert" className="text-sm text-loss sm:col-span-2">
              {general}
            </p>
          )}
          <DialogFooter className="sm:col-span-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={create.isPending}>
              Annulla
            </Button>
            <Button type="submit" disabled={create.isPending}>
              Aggiungi
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// --- instruments ---------------------------------------------------------------------

function useDebounced(value: string, ms: number): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function InstrumentsSection({ tracked, markets }: { tracked: string[]; markets: string[] }) {
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const { data, isLoading, isError } = useInstruments(useDebounced(query.trim(), 250));
  const track = useTrack();
  const trackedSet = new Set(tracked);
  const columns: Column<Instrument>[] = [
    { key: "symbol", header: "Simbolo", sortValue: (i) => i.symbol, cell: (i) => <span className="num font-medium">{i.symbol}</span> },
    { key: "name", header: "Nome", sortValue: (i) => i.name, cell: (i) => i.name },
    { key: "class", header: "Classe", sortValue: (i) => i.asset_class, cell: (i) => i.asset_class },
    { key: "market", header: "Mercato", sortValue: (i) => i.market, cell: (i) => i.market },
    { key: "ccy", header: "Valuta", cell: (i) => i.currency },
    {
      key: "act",
      header: "",
      align: "right",
      cell: (i) => {
        const on = trackedSet.has(i.symbol);
        return (
          <Button
            type="button"
            size="sm"
            variant={on ? "outline" : "default"}
            aria-label={`${on ? "Smetti di seguire" : "Segui"} ${i.symbol}`}
            disabled={track.isPending}
            onClick={() => track.mutate({ symbol: i.symbol, track: !on })}
          >
            {on ? "Smetti di seguire" : "Segui"}
          </Button>
        );
      },
    },
  ];
  return (
    <Card className="mt-6">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>Strumenti</CardTitle>
        <Button type="button" size="sm" onClick={() => setAdding(true)} disabled={markets.length === 0}>
          <Plus className="mr-1 h-4 w-4" />
          Aggiungi strumento
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        <Input aria-label="Cerca strumento" placeholder="Cerca per simbolo o nome" value={query} onChange={(e) => setQuery(e.target.value)} />
        {isError && <p className="text-loss">Impossibile caricare gli strumenti</p>}
        {track.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(track.error)}
          </p>
        )}
        <DataTable aria-label="Strumenti" columns={columns} rows={data} rowKey={(i) => i.symbol} loading={isLoading} empty="Nessuno strumento trovato" />
      </CardContent>
      {adding && <AddInstrumentDialog markets={markets} onClose={() => setAdding(false)} />}
    </Card>
  );
}

// --- live quotes ---------------------------------------------------------------------

function QuotesSection({ tracked }: { tracked: string[] }) {
  const symbols = useMemo(() => tracked.slice(0, MAX_GRID_SYMBOLS), [tracked]);
  const key = symbols.join(",");
  const { socket } = useLiveSocket();
  const snapshot = useQuotes(symbols);
  const [live, setLive] = useState<Record<string, Quote>>({});

  useEffect(() => {
    if (!socket || symbols.length === 0) return;
    return socket.subscribeQuotes(symbols);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` is the stable identity of `symbols`
  }, [socket, key]);
  useLiveMessage("quote", (q) => {
    if (symbols.includes(q.symbol)) setLive((l) => ({ ...l, [q.symbol]: q as unknown as Quote }));
  });

  const quotes = symbols.map((s): Quote | null => {
    const snap = snapshot.data?.[s];
    const base = snap && !("error" in snap) ? snap : null;
    return live[s] ?? base;
  });
  const price = (v: string | null | undefined) => (v ? formatQty(v) : "—");
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Quote in tempo reale</CardTitle>
      </CardHeader>
      <CardContent>
        {symbols.length === 0 ? (
          <p className="text-muted">Nessun simbolo seguito</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {symbols.map((s, i) => (
              <div key={s} className="rounded-md border border-border p-3" aria-label={`Quota ${s}`}>
                <div className="flex items-center justify-between">
                  <span className="font-medium">{s}</span>
                  <span className="text-xs text-muted">{quotes[i]?.provider ?? ""}</span>
                </div>
                <div className="num mt-1 text-lg">{price(quotes[i]?.last)}</div>
                <div className="num text-xs text-muted">
                  Bid {price(quotes[i]?.bid)} · Ask {price(quotes[i]?.ask)}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --- market hours --------------------------------------------------------------------

function HoursSection({ rows }: { rows: MarketStatus[] | undefined }) {
  const columns: Column<MarketStatus>[] = [
    { key: "market", header: "Mercato", sortValue: (m) => m.market, cell: (m) => m.market },
    {
      key: "status",
      header: "Stato",
      cell: (m) => <Badge variant={m.is_open ? "default" : "secondary"}>{m.is_open ? "Aperto" : "Chiuso"}</Badge>,
    },
    { key: "open", header: "Prossima apertura", cell: (m) => <span className="num">{m.next_open ? formatDateTime(m.next_open) : "—"}</span> },
    { key: "close", header: "Prossima chiusura", cell: (m) => <span className="num">{m.next_close ? formatDateTime(m.next_close) : "—"}</span> },
  ];
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Orari dei mercati</CardTitle>
      </CardHeader>
      <CardContent>
        <DataTable aria-label="Orari dei mercati" columns={columns} rows={rows} rowKey={(m) => m.market} loading={!rows} />
      </CardContent>
    </Card>
  );
}

export default function Markets() {
  const stream = useStreamStatus();
  const hours = useMarketsStatus();
  const tracked = useMemo(() => stream.data?.tracked ?? [], [stream.data]);
  const markets = (hours.data ?? []).map((m) => m.market);
  return (
    <div>
      <PageHeader title="Mercati" />
      <QuotesSection tracked={tracked} />
      <InstrumentsSection tracked={tracked} markets={markets} />
      <HoursSection rows={hours.data} />
    </div>
  );
}
