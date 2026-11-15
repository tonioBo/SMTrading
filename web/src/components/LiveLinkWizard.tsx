import { OctagonX, ShieldAlert } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import {
  useCreateLiveLink,
  useDisarmLiveLink,
  useReplaceLiveKeys,
  useSetLiveDryRun,
  useTestLiveLink,
} from "@/api/hooks/live";
import { usePortfolios, useRiskProfiles, useValuation } from "@/api/hooks/portfolios";
import type { LiveKeysInput, LiveKind, LiveLink, LiveSnapshot, LiveState, LiveTestResult, RiskProfile } from "@/api/types";
import { FormField } from "@/components/FormField";
import { LiveArmDialog } from "@/components/LiveArmDialog";
import { LiveLinkPortfolios } from "@/components/LiveLinkPortfolios";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { SECRET_LABELS } from "@/lib/broker";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime, formatMoney } from "@/lib/format";
import {
  DISARM_LABEL,
  DISARM_RETRY_LABEL,
  DISARM_UNSETTLED,
  LIVE_KINDS,
  LIVE_STATE_LABELS,
  T212_PERMISSIONS,
  liveKindInfo,
} from "@/lib/live";
import { cn } from "@/lib/utils";
import { AdminOnly } from "@/components/AdminOnly";
import { useIsAdmin } from "@/api/hooks/auth";

function Alert({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="text-sm text-loss">
      {children}
    </p>
  );
}

/** Minimum permissions and IP restriction, per broker (step 1). */
function KeysGuide({ kind }: { kind: string }) {
  if (kind === "etoro_live") {
    return (
      <div className="space-y-1 rounded-md border border-border bg-card p-3 text-xs">
        <p>
          Crea la chiave utente per l'ambiente <strong>Real</strong> con permesso <strong>Write</strong> e incolla qui la
          chiave API e la chiave utente di quell'ambiente. Non riusare la chiave del collegamento demo: ogni chiave vale per
          un solo ambiente.
        </p>
        <p>
          Usa un conto eToro dedicato a SMTrading, senza posizioni, ordini o copy trading: SMTrading apre solo posizioni
          senza leva e le chiude dal più vecchio.
        </p>
        <p>
          Guida alle chiavi:{" "}
          <a
            className="underline"
            href="https://api-portal.etoro.com/core/getting-started/authentication.md"
            target="_blank"
            rel="noreferrer"
          >
            portale API di eToro
          </a>
          .
        </p>
      </div>
    );
  }
  if (kind === "t212_live") {
    return (
      <div className="space-y-1 rounded-md border border-border bg-card p-3 text-xs">
        <p className="font-medium">Permessi minimi della chiave API di Trading 212:</p>
        <ul className="list-disc pl-5">
          {T212_PERMISSIONS.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
        <p>
          Non concedere <strong>«Pies - Write»</strong>. Restrizione IP consigliata: limita la chiave all'indirizzo IP pubblico da cui
          gira SMTrading.
        </p>
      </div>
    );
  }
  return (
    <div className="space-y-1 rounded-md border border-border bg-card p-3 text-xs">
      <p>
        Usa le chiavi del conto <strong>live</strong> Alpaca (non paper), su un conto cash dedicato a SMTrading, senza posizioni né
        ordini aperti. Se il tuo accesso lo consente, limita le chiavi all'indirizzo IP pubblico di SMTrading.
      </p>
    </div>
  );
}

function KeyFields({
  kind,
  values,
  onChange,
  error,
}: {
  kind: string;
  values: Record<string, string>;
  onChange: (k: string, v: string) => void;
  error: unknown;
}) {
  const info = liveKindInfo(kind);
  const keys = info?.keys ?? [];
  const field = errorField(error);
  return (
    <>
      {keys.map((k) => (
        <FormField key={k} label={info?.labels?.[k] ?? SECRET_LABELS[k] ?? k} error={field === k ? errorMessage(error) : null}>
          <Input type="password" autoComplete="new-password" value={values[k] ?? ""} onChange={(e) => onChange(k, e.target.value)} />
        </FormField>
      ))}
    </>
  );
}

function keysBody(kind: string, values: Record<string, string>): LiveKeysInput {
  const body: Record<string, string> = {};
  for (const k of liveKindInfo(kind)?.keys ?? []) if (values[k]?.trim()) body[k] = values[k].trim();
  return body as LiveKeysInput;
}

// --- step 1 for a new link --------------------------------------------------------------

/** Step 1 «Chiavi» of a new real-money link: the link is created `configured`. */
export function NewLiveLinkForm({ onDone }: { onDone: () => void }) {
  const create = useCreateLiveLink();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<LiveKind>("alpaca_live");
  const [values, setValues] = useState<Record<string, string>>({});
  const [crypto, setCrypto] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const field = errorField(create.error);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return setError("Il nome è obbligatorio");
    setError(null);
    const body: LiveKeysInput = { name: name.trim(), kind, ...keysBody(kind, values) };
    if (kind === "alpaca_live") body.crypto_enabled = crypto;
    create.mutate(body, {
      onSuccess: (link) => {
        toast.success(`Collegamento reale ${link.name} creato: esegui il test in sola lettura`);
        setValues({});
        create.reset(); // drop the keys held in the mutation state
        onDone();
      },
    });
  }

  const keys = liveKindInfo(kind)?.keys ?? [];
  const general = create.error && !(field && (field === "name" || keys.includes(field))) ? errorMessage(create.error) : null;
  return (
    <Card className="mb-6 border-2 border-loss">
      <CardHeader>
        <CardTitle className="text-base">Nuovo collegamento con denaro reale · 1. Chiavi</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Nuovo collegamento denaro reale" className="grid gap-3 md:grid-cols-2" onSubmit={submit}>
          <FormField label="Nome" error={error ?? (field === "name" ? errorMessage(create.error) : null)}>
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </FormField>
          <fieldset className="space-y-2 md:col-span-2">
            <legend className="mb-1 text-sm font-medium">Tipo di conto</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {LIVE_KINDS.map((k) => (
                <label
                  key={k.value}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded-md border-2 border-loss px-3 py-2 text-sm",
                    kind === k.value && "bg-loss/10 font-semibold",
                  )}
                >
                  <input
                    type="radio"
                    name="live-kind"
                    value={k.value}
                    checked={kind === k.value}
                    onChange={() => {
                      setKind(k.value);
                      setValues({});
                    }}
                    className="accent-[rgb(var(--loss))]"
                  />
                  {k.label}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="md:col-span-2">
            <KeysGuide kind={kind} />
          </div>
          <KeyFields kind={kind} values={values} onChange={(k, v) => setValues((s) => ({ ...s, [k]: v }))} error={create.error} />
          {kind === "alpaca_live" && (
            <div className="flex items-center gap-2 text-sm md:col-span-2">
              <input id="live-crypto" type="checkbox" checked={crypto} onChange={(e) => setCrypto(e.target.checked)} className="h-4 w-4" />
              <label htmlFor="live-crypto">Cripto (solo se attiva sul conto; disattivata di default)</label>
            </div>
          )}
          {general && (
            <div className="md:col-span-2">
              <Alert>{general}</Alert>
            </div>
          )}
          <div className="flex gap-2 md:col-span-2">
            <Button type="submit" size="sm" variant="destructive" disabled={create.isPending}>
              Salva chiavi
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

// --- the steps of an existing link --------------------------------------------------------

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="space-y-2 border-t border-border pt-3" aria-label={`Passo ${n}: ${title}`}>
      <h3 className="text-sm font-semibold">
        {n}. {title}
      </h3>
      {children}
    </li>
  );
}

function ReplaceKeys({ link }: { link: LiveLink }) {
  const replace = useReplaceLiveKeys(link.id);
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const keys = liveKindInfo(link.kind)?.keys ?? [];
  const field = errorField(replace.error);

  function save() {
    replace.mutate(keysBody(link.kind, values), {
      onSuccess: () => {
        setValues({});
        setOpen(false);
        replace.reset();
        toast.success(`Chiavi di ${link.name} sostituite: esegui di nuovo il test`);
      },
    });
  }

  if (!open) {
    return (
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}>
        Sostituisci chiavi
      </Button>
    );
  }
  return (
    <div className="space-y-2">
      <KeysGuide kind={link.kind} />
      <KeyFields kind={link.kind} values={values} onChange={(k, v) => setValues((s) => ({ ...s, [k]: v }))} error={replace.error} />
      <p className="text-xs text-muted">Le nuove chiavi riportano il collegamento a «sospeso»: servono un nuovo test e un nuovo TOTP.</p>
      {replace.error && !(field && keys.includes(field)) && <Alert>{errorMessage(replace.error)}</Alert>}
      <div className="flex gap-2">
        <Button type="button" size="sm" variant="destructive" onClick={save} disabled={replace.isPending}>
          Salva nuove chiavi
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => {
            setValues({});
            setOpen(false);
            replace.reset();
          }}
        >
          Annulla
        </Button>
      </div>
    </div>
  );
}

function Count({ value }: { value: number | null | undefined }) {
  if (value == null) return <>—</>;
  return <span className={cn("num", value === 0 ? "text-gain" : "font-semibold text-loss")}>{value}</span>;
}

function SnapshotView({ kind, snap, test }: { kind: string; snap: LiveSnapshot; test: LiveTestResult | null }) {
  const rows: [string, ReactNode][] = [];
  if (test) rows.push(["Conto", <span className="font-mono">{test.account_masked ?? "—"}</span>]);
  if (test) rows.push(["Tipo di conto", test.account_type ?? "—"]);
  rows.push(["Valuta", snap.currency ?? "—"]);
  rows.push([
    "Liquidità disponibile",
    snap.cash_available != null && snap.currency ? formatMoney(snap.cash_available, snap.currency) : (snap.cash_available ?? "—"),
  ]);
  rows.push(["Posizioni aperte", <Count value={snap.positions_count} />]);
  rows.push(["Ordini aperti", <Count value={snap.open_orders_count} />]);
  if (kind === "alpaca_live") rows.push(["Cripto", snap.crypto_enabled == null ? "—" : snap.crypto_enabled ? "sì" : "no"]);
  rows.push(["Tipi d'ordine", snap.order_types?.length ? snap.order_types.join(", ") : "—"]);
  return (
    <dl aria-label="Risultato del test" className="divide-y divide-border text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4 py-1">
          <dt className="text-muted">{label}</dt>
          <dd className="text-right">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function TestStep({ link }: { link: LiveLink }) {
  const test = useTestLiveLink(link.id);
  const snap: LiveSnapshot | null = test.data ?? link.snapshot;
  const empty = snap ? snap.positions_count === 0 && snap.open_orders_count === 0 : null;
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">
        Solo letture (GET): nessun ordine. Il conto deve avere <strong>zero posizioni e zero ordini aperti</strong>. Il test vale 15
        minuti per attivare o collegare.
      </p>
      <AdminOnly>
        <Button type="button" size="sm" variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? "Test in corso…" : "Esegui il test in sola lettura"}
        </Button>
      </AdminOnly>
      {test.error && <Alert>{errorMessage(test.error)}</Alert>}
      {snap ? (
        <>
          {!test.data && link.last_test_at && <p className="text-xs text-muted">Ultimo test: {formatDateTime(link.last_test_at)}</p>}
          <SnapshotView kind={link.kind} snap={snap} test={test.data ?? null} />
          {test.data?.account_changed && (
            <Alert>Conto diverso da quello del test precedente: denaro reale sospeso, ripeti il test e la verifica.</Alert>
          )}
          {empty === false && <Alert>Il conto non è dedicato: chiudi posizioni e ordini dall'app del broker (servono zero posizioni e zero ordini aperti).</Alert>}
          {empty && test.data && (
            <p role="status" className="text-sm text-gain">
              Conto vuoto: test superato
            </p>
          )}
        </>
      ) : (
        <p className="text-sm text-muted">Test non ancora eseguito</p>
      )}
    </div>
  );
}

function DryRunStep({ link }: { link: LiveLink }) {
  const dry = useSetLiveDryRun(link.id);
  const admin = useIsAdmin();
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted">
        Facoltativa, consigliata: gli ordini passano da rischio e invio ma si fermano prima del broker (rifiutati con il payload
        salvato). Le letture sono vere.
      </p>
      {link.live_state === "configured" && admin && (
        <Button type="button" size="sm" variant="outline" disabled={dry.isPending} onClick={() => dry.mutate(true, { onSuccess: () => toast.success("Prova a secco avviata") })}>
          Avvia la prova a secco
        </Button>
      )}
      {link.live_state === "dry_run" && admin && (
        <Button type="button" size="sm" variant="outline" disabled={dry.isPending} onClick={() => dry.mutate(false, { onSuccess: () => toast.success("Prova a secco terminata") })}>
          Termina la prova a secco
        </Button>
      )}
      {dry.error && <Alert>{errorMessage(dry.error)}</Alert>}
      <LiveLinkPortfolios link={link} />
    </div>
  );
}

/** The arm dialog with {liquidità} and {profilo} of the linked portfolio. */
function ArmWithPortfolio({ link, portfolioId, onClose }: { link: LiveLink; portfolioId: string; onClose: () => void }) {
  const portfolios = usePortfolios();
  const profiles = useRiskProfiles();
  const valuation = useValuation(portfolioId);
  const pf = portfolios.data?.find((p) => p.id === portfolioId);
  const ccy = pf?.base_currency ?? link.snapshot?.currency ?? "EUR";
  const amount = valuation.data?.cash?.[ccy];
  const cash = amount != null ? formatMoney(amount, ccy) : valuation.isError ? "non disponibile" : "…";
  return <LiveArmDialog link={link} cash={cash} profile={profileName(pf?.risk_profile_id, profiles.data)} onClose={onClose} />;
}

function profileName(id: string | null | undefined, profiles: RiskProfile[] | undefined): string {
  if (!profiles) return "…";
  return id ? (profiles.find((p) => p.id === id)?.name ?? "…") : (profiles.find((p) => p.name === "Default")?.name ?? "Default");
}

/** Several portfolios on the account: {liquidità} is the sum of their quotas, {profilo} each one's profile. */
function ArmWithPortfolios({ link, onClose }: { link: LiveLink; onClose: () => void }) {
  const portfolios = usePortfolios();
  const profiles = useRiskProfiles();
  const n = link.portfolios.length;
  const total = link.quotas_total != null && link.currency ? formatMoney(link.quotas_total, link.currency) : "non disponibile";
  const profile = link.portfolios
    .map((lp) => `${lp.name}: ${profileName(portfolios.data?.find((p) => p.id === lp.id)?.risk_profile_id, profiles.data)}`)
    .join(" · ");
  return <LiveArmDialog link={link} cash={`${total} (quote di ${n} portafogli)`} profile={profile} onClose={onClose} />;
}

function ArmStep({ link }: { link: LiveLink }) {
  const [open, setOpen] = useState(false);
  const armed = link.live_state === "armed";
  return (
    <div className="space-y-2">
      {armed ? (
        <p className="text-sm font-semibold text-loss">
          Denaro reale attivo{link.armed_at ? ` dal ${formatDateTime(link.armed_at)}` : ""}
          {link.armed_by ? ` (${link.armed_by})` : ""}
        </p>
      ) : (
        <p className="text-xs text-muted">Richiede il test in sola lettura degli ultimi 15 minuti, Telegram funzionante e il TOTP.</p>
      )}
      <AdminOnly>
        <Button type="button" size="sm" variant="destructive" disabled={armed} onClick={() => setOpen(true)}>
          <ShieldAlert aria-hidden />
          Attiva denaro reale
        </Button>
      </AdminOnly>
      {open &&
        (link.portfolios.length === 1 ? (
          <ArmWithPortfolio link={link} portfolioId={link.portfolios[0].id} onClose={() => setOpen(false)} />
        ) : link.portfolios.length > 1 ? (
          <ArmWithPortfolios link={link} onClose={() => setOpen(false)} />
        ) : (
          <LiveArmDialog link={link} cash="— (nessun portafoglio collegato)" profile="—" onClose={() => setOpen(false)} />
        ))}
    </div>
  );
}

/**
 * «Disattiva denaro reale»: one click, no confirmation, no TOTP. On a link already
 * `configured` that was armed before (`everArmed`) it is the idempotent retry (settles the
 * orders left by a failed disarm), reachable after a reload or from another device; a link
 * never armed shows nothing.
 */
export function LiveDisarmButton({
  linkId,
  linkName,
  liveState,
  everArmed,
}: {
  linkId: string;
  linkName: string;
  liveState: LiveState;
  everArmed: boolean;
}) {
  const disarm = useDisarmLiveLink(linkId);
  const [unsettled, setUnsettled] = useState(false);
  const retry = liveState === "configured";
  if (retry && !everArmed && !unsettled) return null;
  return (
    <div className="space-y-1">
      <Button
        type="button"
        variant={retry ? "outline" : "destructive"}
        size={retry ? "sm" : "default"}
        className={retry ? "w-full border-loss text-loss" : "w-full font-semibold"}
        disabled={disarm.isPending}
        onClick={() =>
          disarm.mutate(undefined, {
            onSuccess: (res) => {
              setUnsettled(!res.settled);
              if (res.settled) toast.success(`Denaro reale disattivato su ${linkName}`);
              else toast.warning(DISARM_UNSETTLED);
            },
          })
        }
      >
        <OctagonX aria-hidden />
        {retry ? DISARM_RETRY_LABEL : DISARM_LABEL}
      </Button>
      {unsettled && <Alert>{DISARM_UNSETTLED}</Alert>}
      {disarm.error && <Alert>{errorMessage(disarm.error)}</Alert>}
    </div>
  );
}

/** A real-money link and its 4 steps: keys, read-only test, dry run, arm. */
export function LiveLinkWizard({ link }: { link: LiveLink }) {
  const info = liveKindInfo(link.kind);
  const armed = link.live_state === "armed";
  return (
    <Card aria-label={`Denaro reale ${link.name}`} className="border-2 border-loss">
      <CardHeader className="space-y-2">
        <LiveDisarmButton
          linkId={link.id}
          linkName={link.name}
          liveState={link.live_state}
          everArmed={Boolean(link.armed_at || link.disarmed_at)}
        />
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{link.name}</CardTitle>
            <p className="mt-1 text-xs text-muted">{info?.label ?? link.kind}</p>
          </div>
          <Badge variant={armed ? "destructive" : link.live_state === "dry_run" ? "warning" : "outline"}>
            {LIVE_STATE_LABELS[link.live_state] ?? link.live_state}
          </Badge>
        </div>
        {link.disarmed_reason && link.live_state === "configured" && (
          <p className="text-xs text-muted">
            Disattivato{link.disarmed_at ? ` il ${formatDateTime(link.disarmed_at)}` : ""}: {link.disarmed_reason}
          </p>
        )}
        {link.health_failures > 0 && <p className="text-xs text-loss">Errori di connessione consecutivi: {link.health_failures}</p>}
      </CardHeader>
      <CardContent>
        <ol className="space-y-3">
          <Step n={1} title="Chiavi">
            <p className="text-sm">
              Chiave salvata: <span className="font-mono">{link.key_masked}</span>
              {link.account_masked && (
                <>
                  {" "}
                  · conto <span className="font-mono">{link.account_masked}</span>
                </>
              )}
            </p>
            <AdminOnly>
              <ReplaceKeys link={link} />
            </AdminOnly>
          </Step>
          <Step n={2} title="Test in sola lettura">
            <TestStep link={link} />
          </Step>
          <Step n={3} title="Prova a secco">
            <DryRunStep link={link} />
          </Step>
          <Step n={4} title="Attiva denaro reale">
            <ArmStep link={link} />
          </Step>
        </ol>
      </CardContent>
    </Card>
  );
}
