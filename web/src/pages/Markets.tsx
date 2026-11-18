import { Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useStreamStatus } from "@/api/hooks/connectors";
import {
  INSTRUMENTS_PAGE,
  useAddMarketClosure,
  useAutoAdd,
  useCreateInstrument,
  useDeleteMarketClosure,
  useInstruments,
  useMarketClosures,
  useMarketsStatus,
  useQuotes,
  useSetAutoAdd,
  useTrack,
} from "@/api/hooks/markets";
import type { AssetClass, Instrument, InstrumentCreate, MarketClosure, MarketClosureCreate, MarketClosureKind, MarketStatus, Quote, TrackSource } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { ExtendedQuoteLine } from "@/components/ExtendedHours";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime, formatQty } from "@/lib/format";
import { useLiveMessage, useLiveSocket } from "@/lib/ws";
import { AdminFieldset, AdminOnly } from "@/components/AdminOnly";
import { useIsAdmin } from "@/api/hooks/auth";

const ASSET_CLASSES: AssetClass[] = ["us_equity", "eu_equity", "crypto", "forex"];
const SYMBOL_RE = /^[A-Z0-9][A-Z0-9.\-_/=^]{0,63}$/;
const MAX_GRID_SYMBOLS = 50;

const isPositive = (v: string | null) => v !== null && /[1-9]/.test(v);

const TRACK_SOURCE_LABELS: Record<TrackSource, string> = {
  orders: "ordini",
  positions: "posizioni",
  alerts: "alert",
  watchlists: "watchlist agenti",
  admin: "admin",
};

const sourceLabels = (sources: TrackSource[]) => sources.map((s) => TRACK_SOURCE_LABELS[s] ?? s).join(", ");

function instrumentSourceLabel(i: Pick<Instrument, "source" | "added_by">): string {
  if (i.source === "catalog") return "catalogo";
  if (i.source === "auto") {
    const agent = i.added_by?.startsWith("auto:") ? i.added_by.slice(5) : i.added_by;
    return agent ? `auto: agente ${agent}` : "auto";
  }
  return "manuale";
}

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

function AutoAddToggle() {
  const setting = useAutoAdd();
  const set = useSetAutoAdd();
  if (!setting.data) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <Switch id="auto-add" aria-label="Aggiunta automatica degli strumenti" checked={setting.data.on} disabled={set.isPending} onCheckedChange={(on) => set.mutate(on)} />
      <label htmlFor="auto-add">Aggiunta automatica degli strumenti richiesti dagli agenti</label>
      <span className="text-xs text-muted">(max {setting.data.daily_limit} al giorno per agente)</span>
      {set.error && (
        <span role="alert" className="text-loss">
          {errorMessage(set.error)}
        </span>
      )}
    </div>
  );
}

function InstrumentsSection({ sources, markets }: { sources: Record<string, TrackSource[]>; markets: string[] }) {
  const [query, setQuery] = useState("");
  const [market, setMarket] = useState("");
  const [offset, setOffset] = useState(0);
  const admin = useIsAdmin();
  const [adding, setAdding] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const { data, isLoading, isError } = useInstruments({ query: useDebounced(query.trim(), 250), market, offset });
  const track = useTrack();
  const total = data?.total ?? 0;

  function toggle(symbol: string, follow: boolean) {
    setNote(null);
    track.mutate(
      { symbol, track: follow },
      {
        onSuccess: (res) => {
          const still = res?.still_tracked?.[symbol];
          if (!follow && still?.length) setNote(`${symbol}: resta seguito perché usato da: ${sourceLabels(still)}`);
        },
      },
    );
  }

  const columns: Column<Instrument>[] = [
    { key: "symbol", header: "Simbolo", sortValue: (i) => i.symbol, cell: (i) => <span className="num font-medium">{i.symbol}</span> },
    { key: "name", header: "Nome", sortValue: (i) => i.name, cell: (i) => i.name },
    { key: "class", header: "Classe", sortValue: (i) => i.asset_class, cell: (i) => i.asset_class },
    { key: "market", header: "Mercato", sortValue: (i) => i.market, cell: (i) => i.market },
    { key: "ccy", header: "Valuta", cell: (i) => i.currency },
    { key: "source", header: "Origine", sortValue: (i) => instrumentSourceLabel(i), cell: (i) => <span className="text-xs">{instrumentSourceLabel(i)}</span> },
    {
      key: "followed",
      header: "Seguito da",
      cell: (i) => <span className="text-xs text-muted">{sources[i.symbol]?.length ? sourceLabels(sources[i.symbol]) : "—"}</span>,
    },
    {
      key: "act",
      header: "",
      align: "right",
      cell: (i) => {
        const byAdmin = sources[i.symbol]?.includes("admin") ?? false;
        if (!admin) return null;
        return (
          <Button
            type="button"
            size="sm"
            variant={byAdmin ? "outline" : "default"}
            aria-label={`${byAdmin ? "Smetti di seguire" : "Segui"} ${i.symbol}`}
            disabled={track.isPending}
            onClick={() => toggle(i.symbol, !byAdmin)}
          >
            {byAdmin ? "Smetti di seguire" : "Segui"}
          </Button>
        );
      },
    },
  ];
  return (
    <Card className="mt-6">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>Strumenti</CardTitle>
        <AdminOnly>
          <Button type="button" size="sm" onClick={() => setAdding(true)} disabled={markets.length === 0}>
            <Plus className="mr-1 h-4 w-4" />
            Aggiungi strumento
          </Button>
        </AdminOnly>
      </CardHeader>
      <CardContent className="space-y-3">
        <AdminFieldset>
          <AutoAddToggle />
        </AdminFieldset>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            aria-label="Cerca strumento"
            placeholder="Cerca per simbolo o nome"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOffset(0);
            }}
          />
          <select
            aria-label="Filtra per mercato"
            className={`${selectClass} sm:w-56`}
            value={market}
            onChange={(e) => {
              setMarket(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Tutti i mercati</option>
            {markets.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
        {isError && <p className="text-loss">Impossibile caricare gli strumenti</p>}
        {track.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(track.error)}
          </p>
        )}
        {note && (
          <p role="status" className="text-sm text-muted">
            {note}
          </p>
        )}
        <DataTable aria-label="Strumenti" columns={columns} rows={data?.items} rowKey={(i) => i.symbol} loading={isLoading} empty="Nessuno strumento trovato" />
        {total > 0 && (
          <div className="flex items-center justify-between text-sm text-muted">
            <span>
              {offset + 1}–{Math.min(offset + INSTRUMENTS_PAGE, total)} di {total}
            </span>
            <div className="flex gap-2">
              <Button type="button" size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - INSTRUMENTS_PAGE))}>
                Precedenti
              </Button>
              <Button type="button" size="sm" variant="outline" disabled={offset + INSTRUMENTS_PAGE >= total} onClick={() => setOffset(offset + INSTRUMENTS_PAGE)}>
                Successivi
              </Button>
            </div>
          </div>
        )}
      </CardContent>
      {adding && <AddInstrumentDialog markets={markets} onClose={() => setAdding(false)} />}
    </Card>
  );
}

// --- live quotes ---------------------------------------------------------------------

/** Bid/ask mid as a decimal string (feeds with quotes only carry no last trade). */
function midPrice(bid: string | null | undefined, ask: string | null | undefined): string | null {
  const b = bid ? Number(bid) : NaN;
  const a = ask ? Number(ask) : NaN;
  if (!Number.isFinite(b) || !Number.isFinite(a) || b <= 0 || a <= 0) return null;
  const decimals = Math.max((bid!.split(".")[1] ?? "").length, (ask!.split(".")[1] ?? "").length) + 1;
  return ((a + b) / 2).toFixed(Math.min(decimals, 8)).replace(/0+$/, "").replace(/\.$/, "");
}

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
  // the live feed carries no extended-hours price: it comes from the snapshot only
  const extended = symbols.map((s) => {
    const snap = snapshot.data?.[s];
    return snap && !("error" in snap) ? snap.extended : null;
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
                <div className="num mt-1 text-lg">
                  {quotes[i]?.last ? (
                    price(quotes[i]?.last)
                  ) : midPrice(quotes[i]?.bid, quotes[i]?.ask) ? (
                    <>
                      {formatQty(midPrice(quotes[i]?.bid, quotes[i]?.ask)!)}{" "}
                      <span className="text-xs text-muted">medio</span>
                    </>
                  ) : (
                    "—"
                  )}
                </div>
                <ExtendedQuoteLine ext={extended[i]} />
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

// --- manual closures -----------------------------------------------------------------

/** Markets without an exchange calendar: a manual closure does not apply to them. */
const NO_CALENDAR_MARKETS = ["CRYPTO", "FX"];
const MAX_NOTE = 200;

/** "2026-11-27" → "27/11/2026" */
const formatDay = (day: string) => day.split("-").reverse().join("/");

function closureKindLabel(c: Pick<MarketClosure, "kind" | "close_time">): string {
  return c.kind === "closed" ? "Chiusa" : `Chiusura anticipata alle ${c.close_time ?? "—"}`;
}

const authorLabel = (by: string) => (by.startsWith("admin:") ? by.slice(6) : by);

function ClosureForm({ markets }: { markets: string[] }) {
  const add = useAddMarketClosure();
  const empty = { market: markets[0] ?? "", day: "", kind: "closed" as MarketClosureKind, close_time: "", note: "" };
  const [form, setForm] = useState(empty);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));
  const market = markets.includes(form.market) ? form.market : (markets[0] ?? "");
  const serverField = errorField(add.error);
  const fieldError = (f: string) => errors[f] ?? (add.error && serverField === f ? errorMessage(add.error) : null);
  const general = add.error && (!serverField || !(serverField in form)) ? errorMessage(add.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!market) errs.market = "Seleziona un mercato";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(form.day)) errs.day = "Indica il giorno";
    if (form.kind === "early_close" && !/^\d{2}:\d{2}$/.test(form.close_time)) errs.close_time = "Indica l'ora della chiusura";
    if (form.note.trim().length > MAX_NOTE) errs.note = `Massimo ${MAX_NOTE} caratteri`;
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const body: MarketClosureCreate = { market, day: form.day, kind: form.kind, note: form.note.trim() };
    if (form.kind === "early_close") body.close_time = form.close_time;
    add.mutate(body, { onSuccess: () => setForm({ ...empty, market }) });
  }

  return (
    <form aria-label="Nuova chiusura" onSubmit={submit} noValidate className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <FormField label="Mercato" error={fieldError("market")}>
        <select className={selectClass} value={market} onChange={(e) => set({ market: e.target.value })}>
          {markets.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </FormField>
      <FormField label="Giorno" error={fieldError("day")}>
        <Input type="date" value={form.day} onChange={(e) => set({ day: e.target.value })} />
      </FormField>
      <FormField label="Tipo" error={fieldError("kind")}>
        <select className={selectClass} value={form.kind} onChange={(e) => set({ kind: e.target.value as MarketClosureKind })}>
          <option value="closed">Chiusa tutto il giorno</option>
          <option value="early_close">Chiusura anticipata</option>
        </select>
      </FormField>
      {form.kind === "early_close" && (
        <FormField label="Ora di chiusura (locale)" error={fieldError("close_time")}>
          <Input type="time" value={form.close_time} onChange={(e) => set({ close_time: e.target.value })} />
        </FormField>
      )}
      <FormField label="Nota" error={fieldError("note")}>
        <Input value={form.note} maxLength={MAX_NOTE} onChange={(e) => set({ note: e.target.value })} />
      </FormField>
      {general && (
        <p role="alert" className="text-sm text-loss sm:col-span-2 lg:col-span-5">
          {general}
        </p>
      )}
      <div className="sm:col-span-2 lg:col-span-5">
        <Button type="submit" size="sm" disabled={add.isPending || markets.length === 0}>
          <Plus className="mr-1 h-4 w-4" />
          Aggiungi chiusura
        </Button>
      </div>
    </form>
  );
}

function ClosuresSection({ markets }: { markets: string[] }) {
  const closures = useMarketClosures();
  const remove = useDeleteMarketClosure();
  const [deleting, setDeleting] = useState<MarketClosure | null>(null);
  const admin = useIsAdmin();
  const columns: Column<MarketClosure>[] = [
    { key: "market", header: "Mercato", sortValue: (c) => c.market, cell: (c) => c.market },
    { key: "day", header: "Giorno", sortValue: (c) => c.day, cell: (c) => <span className="num">{formatDay(c.day)}</span> },
    { key: "kind", header: "Tipo", cell: (c) => closureKindLabel(c) },
    { key: "note", header: "Nota", cell: (c) => c.note || "—" },
    { key: "by", header: "Autore", cell: (c) => <span className="text-xs">{authorLabel(c.created_by)}</span> },
    {
      key: "act",
      header: "",
      align: "right",
      cell: (c) =>
        admin ? (
          <Button type="button" size="sm" variant="outline" aria-label={`Elimina la chiusura ${c.market} del ${formatDay(c.day)}`} onClick={() => setDeleting(c)}>
            <Trash2 className="h-4 w-4" />
          </Button>
        ) : null,
    },
  ];
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Chiusure straordinarie</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted">
          Chiusure o mezze giornate non previste dal calendario della borsa. L&apos;ora è quella locale della borsa; una chiusura non apre mai un giorno festivo.
        </p>
        {closures.isError && <p className="text-loss">Impossibile caricare le chiusure</p>}
        <DataTable
          aria-label="Chiusure straordinarie"
          columns={columns}
          rows={closures.data}
          rowKey={(c) => String(c.id)}
          loading={closures.isLoading}
          empty="Nessuna chiusura straordinaria in programma"
        />
        <AdminOnly>
          <ClosureForm markets={markets} />
        </AdminOnly>
      </CardContent>
      {deleting && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) {
              setDeleting(null);
              remove.reset();
            }
          }}
          title={`Eliminare la chiusura ${deleting.market} del ${formatDay(deleting.day)}?`}
          description="Il mercato torna a seguire il calendario della borsa per quel giorno."
          confirmLabel="Elimina"
          destructive
          pending={remove.isPending}
          error={remove.error ? errorMessage(remove.error) : null}
          onConfirm={() => remove.mutate(deleting.id, { onSuccess: () => setDeleting(null) })}
        />
      )}
    </Card>
  );
}

export default function Markets() {
  const stream = useStreamStatus();
  const hours = useMarketsStatus();
  const tracked = useMemo(() => stream.data?.tracked ?? [], [stream.data]);
  const sources = useMemo(() => stream.data?.tracked_sources ?? {}, [stream.data]);
  const markets = (hours.data ?? []).filter((m) => m.tradable !== false).map((m) => m.market);
  const closureMarkets = (hours.data ?? []).map((m) => m.market).filter((m) => !NO_CALENDAR_MARKETS.includes(m));
  return (
    <div>
      <PageHeader title="Mercati" />
      <QuotesSection tracked={tracked} />
      <InstrumentsSection sources={sources} markets={markets} />
      <HoursSection rows={hours.data} />
      <ClosuresSection markets={closureMarkets} />
    </div>
  );
}
