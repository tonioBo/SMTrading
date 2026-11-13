import { Pencil, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useAppSettings, useInterestRates, useSaveAppSettings, useSaveInterestRates, useSaveRiskProfile } from "@/api/hooks/risk";
import { useRiskProfiles } from "@/api/hooks/portfolios";
import type { AppSettings, AssetClass, InterestKind, InterestRate, RiskProfile, RiskProfileInput } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { KillSwitchButton } from "@/components/KillSwitchButton";
import { PageHeader } from "@/components/PageHeader";
import { TagInput } from "@/components/TagInput";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatQty } from "@/lib/format";

const ASSET_CLASSES: AssetClass[] = ["us_equity", "eu_equity", "crypto", "forex"];

// --- risk profiles ---------------------------------------------------------------

type LimitField = "max_order_notional_eur" | "max_position_pct" | "max_daily_loss_pct" | "max_daily_loss_eur";
const LIMITS: { field: LimitField; label: string }[] = [
  { field: "max_order_notional_eur", label: "Controvalore massimo per ordine (EUR)" },
  { field: "max_position_pct", label: "Posizione massima (% equity)" },
  { field: "max_daily_loss_pct", label: "Perdita giornaliera massima (%)" },
  { field: "max_daily_loss_eur", label: "Perdita giornaliera massima (EUR)" },
];
const PROFILE_FIELDS = ["name", "short_allowed", "allowed_symbols", "allowed_asset_classes", "allowed_markets", ...LIMITS.map((l) => l.field)];

function ProfileDialog({ profile, onClose }: { profile: RiskProfile | null; onClose: () => void }) {
  const [name, setName] = useState(profile?.name ?? "");
  const [limits, setLimits] = useState<Record<LimitField, string>>(() => ({
    max_order_notional_eur: profile?.max_order_notional_eur ?? "",
    max_position_pct: profile?.max_position_pct ?? "",
    max_daily_loss_pct: profile?.max_daily_loss_pct ?? "",
    max_daily_loss_eur: profile?.max_daily_loss_eur ?? "",
  }));
  const [symbols, setSymbols] = useState(profile?.allowed_symbols ?? []);
  const [classes, setClasses] = useState(profile?.allowed_asset_classes ?? []);
  const [markets, setMarkets] = useState(profile?.allowed_markets ?? []);
  const [short, setShort] = useState(profile?.short_allowed ?? true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const save = useSaveRiskProfile();

  const serverField = errorField(save.error);
  const fieldError = (field: string) => errors[field] ?? (save.error && serverField === field ? errorMessage(save.error) : null);
  const generalError = save.error && (!serverField || !PROFILE_FIELDS.includes(serverField)) ? errorMessage(save.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!name.trim()) errs.name = "Il nome è obbligatorio";
    const parsed = {} as Record<LimitField, string | null>;
    for (const { field } of LIMITS) {
      const raw = limits[field].trim();
      parsed[field] = raw ? normalizeAmount(raw) : null;
      if (raw && parsed[field] === null) errs[field] = "Importo non valido";
    }
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const input: RiskProfileInput = {
      name: name.trim(),
      ...parsed,
      allowed_symbols: symbols,
      allowed_asset_classes: classes,
      allowed_markets: markets,
      short_allowed: short,
    };
    save.mutate({ id: profile?.id ?? null, profile: input }, { onSuccess: onClose });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{profile ? "Modifica profilo di rischio" : "Nuovo profilo di rischio"}</DialogTitle>
          <DialogDescription>Un limite vuoto significa nessun limite; una whitelist vuota ammette tutto.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="sm:col-span-2">
            <FormField label="Nome" error={fieldError("name")}>
              <Input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
            </FormField>
          </div>
          {LIMITS.map(({ field, label }) => (
            <FormField key={field} label={label} error={fieldError(field)}>
              <Input value={limits[field]} inputMode="decimal" onChange={(e) => setLimits((l) => ({ ...l, [field]: e.target.value }))} />
            </FormField>
          ))}
          <div className="sm:col-span-2">
            <FormField label="Simboli ammessi" error={fieldError("allowed_symbols")} hint="Invio o virgola per aggiungere">
              <TagInput value={symbols} onChange={setSymbols} placeholder="AAPL" />
            </FormField>
          </div>
          <div className="sm:col-span-2">
            <FormField label="Classi di asset ammesse" error={fieldError("allowed_asset_classes")} hint="us_equity, eu_equity, crypto, forex">
              <TagInput value={classes} onChange={setClasses} placeholder="crypto" />
            </FormField>
          </div>
          <div className="sm:col-span-2">
            <FormField label="Mercati ammessi" error={fieldError("allowed_markets")}>
              <TagInput value={markets} onChange={setMarkets} placeholder="XNAS" />
            </FormField>
          </div>
          <div className="flex items-center gap-2 sm:col-span-2">
            <input id="risk-short" type="checkbox" checked={short} onChange={(e) => setShort(e.target.checked)} className="h-4 w-4 accent-[rgb(var(--accent))]" />
            <label htmlFor="risk-short">Vendite allo scoperto consentite</label>
          </div>
          {generalError && (
            <p role="alert" className="text-sm text-loss sm:col-span-2">
              {generalError}
            </p>
          )}
          <DialogFooter className="sm:col-span-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>
              Annulla
            </Button>
            <Button type="submit" disabled={save.isPending}>
              Salva
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const list = (v: string[]) => (v.length ? v.join(", ") : "tutti");
const limit = (v: string | null, suffix: string) => (v === null ? "—" : `${formatQty(v)}${suffix}`);

function ProfilesSection() {
  const { data, isLoading, isError } = useRiskProfiles();
  const [editing, setEditing] = useState<RiskProfile | "new" | null>(null);
  const columns: Column<RiskProfile>[] = [
    { key: "name", header: "Nome", sortValue: (p) => p.name, cell: (p) => p.name },
    { key: "order", header: "Max ordine", align: "right", cell: (p) => <span className="num">{limit(p.max_order_notional_eur, " €")}</span> },
    { key: "pos", header: "Max posizione", align: "right", cell: (p) => <span className="num">{limit(p.max_position_pct, "%")}</span> },
    {
      key: "loss",
      header: "Perdita giornaliera",
      align: "right",
      cell: (p) => (
        <span className="num">
          {limit(p.max_daily_loss_pct, "%")} / {limit(p.max_daily_loss_eur, " €")}
        </span>
      ),
    },
    { key: "symbols", header: "Simboli", cell: (p) => list(p.allowed_symbols) },
    { key: "classes", header: "Classi", cell: (p) => list(p.allowed_asset_classes) },
    { key: "markets", header: "Mercati", cell: (p) => list(p.allowed_markets) },
    { key: "short", header: "Short", cell: (p) => (p.short_allowed ? "Sì" : "No") },
    {
      key: "actions",
      header: "Azioni",
      align: "right",
      cell: (p) => (
        <Button size="sm" variant="outline" aria-label={`Modifica ${p.name}`} onClick={() => setEditing(p)}>
          <Pencil />
          Modifica
        </Button>
      ),
    },
  ];
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>Profili di rischio</CardTitle>
        <Button onClick={() => setEditing("new")}>
          <Plus />
          Nuovo profilo
        </Button>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {isError && <p className="mb-4 text-loss">Impossibile caricare i profili</p>}
        <DataTable aria-label="Profili di rischio" columns={columns} rows={data} rowKey={(p) => p.id} loading={isLoading} empty="Nessun profilo" />
      </CardContent>
      {editing && <ProfileDialog profile={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </Card>
  );
}

// --- interest rates --------------------------------------------------------------

const KIND_LABEL: Record<InterestKind, string> = { loan: "prestito (valuta)", borrow: "short (classe di asset)" };

function RatesEditor({ initial }: { initial: InterestRate[] }) {
  const [rows, setRows] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveInterestRates();
  const patch = (i: number, p: Partial<InterestRate>) => setRows((r) => r.map((x, j) => (j === i ? { ...x, ...p } : x)));

  function submit() {
    const out: InterestRate[] = [];
    for (const row of rows) {
      const pct = normalizeAmount(row.annual_pct);
      if (!row.key.trim() || pct === null) return setError("Chiave e tasso annuo devono essere validi");
      out.push({ kind: row.kind, key: row.key.trim(), annual_pct: pct });
    }
    if (!out.some((r) => r.kind === "loan" && r.key === "*")) return setError('Serve un tasso di ripiego per i prestiti (chiave "*")');
    setError(null);
    save.mutate(out, { onSuccess: (data) => { setRows(data); toast.success("Tassi salvati"); } });
  }

  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <div key={i} className="grid grid-cols-[1fr_1fr_1fr_auto] items-center gap-2">
          <select aria-label={`Tipo tasso ${i + 1}`} className={selectClass} value={r.kind} onChange={(e) => patch(i, { kind: e.target.value as InterestKind, key: "" })}>
            {(Object.keys(KIND_LABEL) as InterestKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
          <Input aria-label={`Chiave tasso ${i + 1}`} value={r.key} placeholder={r.kind === "loan" ? "EUR oppure *" : "crypto"} onChange={(e) => patch(i, { key: e.target.value })} />
          <Input aria-label={`Tasso annuo % ${i + 1}`} value={r.annual_pct} inputMode="decimal" onChange={(e) => patch(i, { annual_pct: e.target.value })} />
          <Button type="button" variant="outline" size="sm" aria-label={`Rimuovi tasso ${i + 1}`} onClick={() => setRows((x) => x.filter((_, j) => j !== i))}>
            Rimuovi
          </Button>
        </div>
      ))}
      {(error || save.error) && (
        <p role="alert" className="text-sm text-loss">
          {error ?? errorMessage(save.error)}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={() => setRows((r) => [...r, { kind: "loan", key: "", annual_pct: "0" }])}>
          <Plus />
          Aggiungi tasso
        </Button>
        <Button type="button" onClick={submit} disabled={save.isPending}>
          Salva tassi
        </Button>
      </div>
    </div>
  );
}

function RatesSection() {
  const { data, isError } = useInterestRates();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Tassi di interesse</CardTitle>
      </CardHeader>
      <CardContent>
        {isError && <p className="text-loss">Impossibile caricare i tassi</p>}
        {data && <RatesEditor initial={data} />}
      </CardContent>
    </Card>
  );
}

// --- app settings ----------------------------------------------------------------

function SettingsEditor({ initial }: { initial: AppSettings }) {
  const toText = (m: Record<string, number>) => Object.fromEntries(ASSET_CLASSES.map((c) => [c, String(m[c] ?? "")]));
  const [stale, setStale] = useState(() => toText(initial.stale_quote_s));
  const [spread, setSpread] = useState(() => toText(initial.default_spread_bps));
  const [error, setError] = useState<string | null>(null);
  const save = useSaveAppSettings();

  function submit() {
    const toInts = (m: Record<string, string>) => {
      const out: Record<string, number> = {};
      for (const c of ASSET_CLASSES) {
        if (!/^\d+$/.test(m[c].trim())) return null;
        out[c] = Number(m[c].trim());
      }
      return out;
    };
    const stale_quote_s = toInts(stale);
    const default_spread_bps = toInts(spread);
    if (!stale_quote_s || !default_spread_bps) return setError("Inserisci numeri interi per ogni classe");
    setError(null);
    save.mutate({ stale_quote_s, default_spread_bps }, { onSuccess: () => toast.success("Impostazioni salvate") });
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-[1fr_1fr_1fr] items-center gap-2 text-sm">
        <span className="font-medium">Classe di asset</span>
        <span className="font-medium">Quota vecchia dopo (s)</span>
        <span className="font-medium">Spread predefinito (bps)</span>
        {ASSET_CLASSES.map((c) => (
          <div key={c} className="contents">
            <span className="font-mono">{c}</span>
            <Input aria-label={`Quota vecchia ${c}`} value={stale[c]} inputMode="numeric" onChange={(e) => setStale((s) => ({ ...s, [c]: e.target.value }))} />
            <Input aria-label={`Spread ${c}`} value={spread[c]} inputMode="numeric" onChange={(e) => setSpread((s) => ({ ...s, [c]: e.target.value }))} />
          </div>
        ))}
      </div>
      {(error || save.error) && (
        <p role="alert" className="text-sm text-loss">
          {error ?? errorMessage(save.error)}
        </p>
      )}
      <Button type="button" onClick={submit} disabled={save.isPending}>
        Salva impostazioni
      </Button>
    </div>
  );
}

function SettingsSection() {
  const { data, isError } = useAppSettings();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Quote vecchie e spread</CardTitle>
      </CardHeader>
      <CardContent>
        {isError && <p className="text-loss">Impossibile caricare le impostazioni</p>}
        {data && <SettingsEditor initial={data} />}
      </CardContent>
    </Card>
  );
}

export default function Risk() {
  return (
    <div>
      <PageHeader title="Rischio e margine">
        <KillSwitchButton />
      </PageHeader>
      <ProfilesSection />
      <RatesSection />
      <SettingsSection />
    </div>
  );
}
