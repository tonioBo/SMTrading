import { Plus, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { toast } from "sonner";
import {
  useBrokerLinks,
  useBrokerPositions,
  useCreateBrokerLink,
  usePatchBrokerLink,
  useReconcileNow,
  useReconciliations,
  useSetPortfolioBroker,
  useTestBrokerLink,
} from "@/api/hooks/brokers";
import { useLiveLinks } from "@/api/hooks/live";
import { useIdempotencyKey, usePortfolios } from "@/api/hooks/portfolios";
import type { BrokerKind, BrokerLink, BrokerLinkPatch, Reconciliation } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { LiveLinkWizard, NewLiveLinkForm } from "@/components/LiveLinkWizard";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { BROKER_KINDS, LINK_STATUS_LABELS, SECRET_LABELS, brokerKindLabel } from "@/lib/broker";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime, formatQty } from "@/lib/format";
import { isLiveKind } from "@/lib/live";

const PAPER_NOTE = "Solo conti paper/demo: nessun denaro reale";

function statusVariant(status: string) {
  return status === "ok" ? "default" : status === "error" ? "destructive" : "secondary";
}

// --- new link ------------------------------------------------------------------------

function NewLinkForm({ onDone }: { onDone: () => void }) {
  const create = useCreateBrokerLink();
  const key = useIdempotencyKey(); // one per opening of the form
  const [name, setName] = useState("");
  const [kind, setKind] = useState<BrokerKind>("alpaca_paper");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const keys = BROKER_KINDS.find((k) => k.value === kind)?.keys ?? [];

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("Il nome è obbligatorio");
      return;
    }
    setError(null);
    const config: Record<string, string> = {};
    for (const k of keys) if (secrets[k]?.trim()) config[k] = secrets[k].trim();
    create.mutate(
      { body: { name: name.trim(), kind, config }, key },
      {
        onSuccess: (link) => {
          toast.success(`Collegamento ${link.name} creato`);
          create.reset(); // drop the keys held in the mutation state
          onDone();
        },
      },
    );
  }

  const serverError = create.error ? errorMessage(create.error) : null;
  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="text-base">Nuovo collegamento</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Nuovo collegamento broker" className="grid gap-3 md:grid-cols-2" onSubmit={submit}>
          <FormField label="Nome" error={error ?? (errorField(create.error) === "name" ? serverError : null)}>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </FormField>
          <FormField label="Broker">
            <select
              className={selectClass}
              value={kind}
              onChange={(e) => {
                setKind(e.target.value as BrokerKind);
                setSecrets({});
              }}
            >
              {BROKER_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          </FormField>
          {keys.map((k) => (
            <FormField key={k} label={SECRET_LABELS[k] ?? k}>
              <Input
                type="password"
                autoComplete="off"
                value={secrets[k] ?? ""}
                onChange={(e) => setSecrets((s) => ({ ...s, [k]: e.target.value }))}
              />
            </FormField>
          ))}
          {serverError && errorField(create.error) !== "name" && (
            <p role="alert" className="text-sm text-loss md:col-span-2">
              {serverError}
            </p>
          )}
          <div className="flex gap-2 md:col-span-2">
            <Button type="submit" size="sm" disabled={create.isPending}>
              Crea collegamento
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={onDone}>
              Annulla
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// --- link card -----------------------------------------------------------------------

function SecretsForm({ link }: { link: BrokerLink }) {
  const patch = usePatchBrokerLink();
  const [fields, setFields] = useState<Record<string, string>>({});

  function save() {
    const config: Record<string, string> = {};
    for (const [k, v] of Object.entries(fields)) if (v.trim()) config[k] = v.trim();
    if (Object.keys(config).length === 0) return;
    // untouched secrets are omitted: the server keeps them (never re-sent, never re-shown)
    const body: BrokerLinkPatch = { config };
    patch.mutate(
      { id: link.id, patch: body },
      {
        onSuccess: () => {
          setFields({});
          patch.reset(); // drop the keys held in the mutation state
          toast.success(`Chiavi di ${link.name} aggiornate`);
        },
      },
    );
  }

  return (
    <div className="space-y-2">
      {link.secret_keys.map((k) => (
        <FormField key={k} label={SECRET_LABELS[k] ?? k} hint={link.config_masked[k] ? `Salvata: ${link.config_masked[k]}` : "Non impostata"}>
          <Input
            type="password"
            autoComplete="off"
            placeholder="Nuovo valore"
            value={fields[k] ?? ""}
            onChange={(e) => setFields((f) => ({ ...f, [k]: e.target.value }))}
          />
        </FormField>
      ))}
      {patch.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(patch.error)}
        </p>
      )}
      <Button type="button" size="sm" onClick={save} disabled={patch.isPending}>
        Salva chiavi
      </Button>
    </div>
  );
}

function PortfolioLinks({ link }: { link: BrokerLink }) {
  const portfolios = usePortfolios();
  const setLink = useSetPortfolioBroker();
  const [selected, setSelected] = useState("");
  const available = (portfolios.data ?? []).filter((p) => !p.broker_link_id);

  function assign(portfolioId: string, linkId: string | null, done: string) {
    setLink.mutate(
      { portfolioId, linkId },
      {
        onSuccess: () => {
          setSelected("");
          toast.success(done);
        },
      },
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">Portafogli collegati</p>
      {link.portfolios.length === 0 ? (
        <p className="text-sm text-muted">Nessun portafoglio collegato</p>
      ) : (
        <ul className="space-y-1">
          {link.portfolios.map((p) => (
            <li key={p.id} className="flex items-center justify-between gap-2 text-sm">
              <span>{p.name}</span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                aria-label={`Scollega ${p.name}`}
                disabled={setLink.isPending}
                onClick={() => assign(p.id, null, `${p.name} scollegato`)}
              >
                Scollega
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <FormField label="Collega un portafoglio">
            <select className={selectClass} value={selected} disabled={!link.enabled} onChange={(e) => setSelected(e.target.value)}>
              <option value="">Seleziona…</option>
              {available.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </FormField>
        </div>
        <Button
          type="button"
          size="sm"
          disabled={!selected || setLink.isPending}
          onClick={() => assign(selected, link.id, "Portafoglio collegato")}
        >
          Collega
        </Button>
      </div>
      <p className="text-xs text-muted">
        Si collegano solo portafogli senza posizioni aperte né ordini aperti: il broker non conosce le posizioni simulate.
      </p>
      {setLink.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(setLink.error)}
        </p>
      )}
    </div>
  );
}

function BrokerPositions({ link }: { link: BrokerLink }) {
  const [open, setOpen] = useState(false);
  const { data, isFetching, error, refetch } = useBrokerPositions(link.id, open);
  return (
    <div className="space-y-2">
      <Button type="button" size="sm" variant="outline" onClick={() => (open ? void refetch() : setOpen(true))} disabled={isFetching}>
        {open ? "Aggiorna posizioni del broker" : "Mostra posizioni del broker"}
      </Button>
      {error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(error)}
        </p>
      )}
      {data &&
        (data.positions.length === 0 ? (
          <p className="text-sm text-muted">Nessuna posizione presso il broker</p>
        ) : (
          <ul aria-label={`Posizioni del broker ${link.name}`} className="space-y-1 text-sm">
            {data.positions.map((p) => (
              <li key={p.symbol} className="flex justify-between">
                <span>{p.symbol}</span>
                <span className="num">{formatQty(p.qty)}</span>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}

function LinkCard({ link }: { link: BrokerLink }) {
  const patch = usePatchBrokerLink();
  const test = useTestBrokerLink();
  return (
    <Card aria-label={`Broker ${link.name}`}>
      <CardHeader className="flex-row items-start justify-between gap-2 space-y-0">
        <div>
          <CardTitle className="text-base">{link.name}</CardTitle>
          <p className="mt-1 text-xs text-muted">
            {brokerKindLabel(link.kind)} · <span className="font-mono">{link.base_url}</span>
          </p>
        </div>
        <Switch
          aria-label={`Attiva ${link.name}`}
          checked={link.enabled}
          disabled={patch.isPending}
          onCheckedChange={(enabled) => patch.mutate({ id: link.id, patch: { enabled } })}
        />
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={statusVariant(link.status)}>{LINK_STATUS_LABELS[link.status] ?? link.status}</Badge>
          {!link.enabled && <Badge variant="outline">Disattivato</Badge>}
        </div>
        {link.last_error && <p className="text-xs text-loss">Ultimo errore: {link.last_error}</p>}
        <SecretsForm link={link} />
        <div className="space-y-2">
          <Button type="button" size="sm" variant="outline" onClick={() => test.mutate(link.id)} disabled={test.isPending}>
            Prova connessione
          </Button>
          {test.data && (
            <p role="status" className={test.data.ok ? "text-sm text-gain" : "text-sm text-loss"}>
              {test.data.ok ? "Connessione riuscita" : `Test fallito: ${test.data.error ?? "errore sconosciuto"}`}
            </p>
          )}
          {test.error && (
            <p role="alert" className="text-sm text-loss">
              {errorMessage(test.error)}
            </p>
          )}
        </div>
        <PortfolioLinks link={link} />
        <BrokerPositions link={link} />
      </CardContent>
    </Card>
  );
}

// --- reconciliations -----------------------------------------------------------------

function Reconciliations({ link }: { link: BrokerLink }) {
  const { data, isLoading, isError } = useReconciliations(link.id);
  const run = useReconcileNow(link.id);
  const columns: Column<Reconciliation>[] = [
    { key: "ts", header: "Data", cell: (r) => <span className="num">{formatDateTime(r.ts)}</span> },
    { key: "portfolio", header: "Portafoglio", cell: (r) => r.portfolio_name ?? r.portfolio_id },
    {
      key: "ok",
      header: "Esito",
      cell: (r) => <Badge variant={r.ok ? "default" : "destructive"}>{r.ok ? "Allineato" : "Differenze"}</Badge>,
    },
    {
      key: "diffs",
      header: "Differenze (nostro / broker)",
      cell: (r) =>
        r.diffs.length === 0 ? (
          "—"
        ) : (
          <ul className="space-y-0.5">
            {r.diffs.map((d) => (
              <li key={d.symbol} data-diff className="rounded bg-loss/10 px-1 font-medium text-loss">
                {d.symbol}: <span className="num">{formatQty(d.ours)}</span> / <span className="num">{formatQty(d.broker)}</span>
              </li>
            ))}
          </ul>
        ),
    },
  ];
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="text-base">Riconciliazioni · {link.name}</CardTitle>
        <Button type="button" size="sm" variant="outline" onClick={() => run.mutate()} disabled={run.isPending || !link.enabled}>
          Riconcilia ora
        </Button>
      </CardHeader>
      <CardContent className="space-y-2">
        {run.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(run.error)}
          </p>
        )}
        {isError && <p className="text-sm text-loss">Impossibile caricare le riconciliazioni</p>}
        <DataTable
          aria-label={`Riconciliazioni ${link.name}`}
          columns={columns}
          rows={data}
          rowKey={(r) => r.id}
          loading={isLoading}
          empty="Nessuna riconciliazione"
        />
      </CardContent>
    </Card>
  );
}

// --- real money ----------------------------------------------------------------------

function LiveSection() {
  const { data, isLoading, isError } = useLiveLinks();
  const [creating, setCreating] = useState(false);
  const { hash } = useLocation();
  const loaded = Boolean(data);
  useEffect(() => {
    // from the red banner's «Disattiva»: bring the disarm buttons into view
    if (hash === "#denaro-reale" && loaded) document.getElementById("denaro-reale")?.scrollIntoView?.({ block: "start" });
  }, [hash, loaded]);
  return (
    <section id="denaro-reale" aria-labelledby="live-heading" className="mt-10 scroll-mt-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id="live-heading" className="flex items-center gap-2 text-lg font-semibold text-loss">
          <ShieldAlert className="h-5 w-5" aria-hidden />
          Denaro reale
        </h2>
        {!creating && (
          <Button size="sm" variant="destructive" onClick={() => setCreating(true)}>
            <Plus className="mr-1 h-4 w-4" aria-hidden />
            Nuovo collegamento reale
          </Button>
        )}
      </div>
      <p className="mb-4 text-sm text-muted">
        Procedura in 4 passi: chiavi, test in sola lettura, prova a secco (facoltativa), attivazione con TOTP. «Disattiva denaro reale»
        è sempre a un clic.
      </p>
      {creating && <NewLiveLinkForm onDone={() => setCreating(false)} />}
      {isError && <p className="text-loss">Impossibile caricare i collegamenti reali</p>}
      {isLoading && <p className="text-muted">Caricamento…</p>}
      {data && data.length === 0 && !creating && <p className="text-muted">Nessun collegamento con denaro reale</p>}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data?.map((l) => (
          <LiveLinkWizard key={l.id} link={l} />
        ))}
      </div>
    </section>
  );
}

// --- page ----------------------------------------------------------------------------

export default function Brokers() {
  const { data: all, isLoading, isError } = useBrokerLinks();
  const data = all?.filter((l) => !isLiveKind(l.kind)); // real-money links: the section below
  const [creating, setCreating] = useState(false);
  return (
    <div>
      <PageHeader title="Broker">
        {!creating && (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="mr-1 h-4 w-4" aria-hidden />
            Nuovo collegamento
          </Button>
        )}
      </PageHeader>
      <p role="note" className="mb-6 flex items-center gap-2 rounded-md border border-warn bg-warn/10 px-3 py-2 text-sm font-medium">
        <ShieldCheck className="h-4 w-4" aria-hidden />
        {PAPER_NOTE}
      </p>
      {creating && <NewLinkForm onDone={() => setCreating(false)} />}
      {isError && <p className="text-loss">Impossibile caricare i collegamenti broker</p>}
      {isLoading && <p className="text-muted">Caricamento…</p>}
      {data && data.length === 0 && !creating && <p className="text-muted">Nessun collegamento broker</p>}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data?.map((l) => (
          <LinkCard key={l.id} link={l} />
        ))}
      </div>
      <LiveSection />
      {all && all.length > 0 && (
        <div className="mt-6 space-y-4">
          {all.map((l) => (
            <Reconciliations key={l.id} link={l} />
          ))}
        </div>
      )}
    </div>
  );
}
