import { Pencil, Plus, PowerOff } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useDeactivateFeeRule, useFeeRules, useSaveFeeRule, useSimulateFee } from "@/api/hooks/fees";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { AssetClass, FeeRule, FeeRuleInput, FeeScope, FeeSide, FeeTier } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { AmountPreview } from "@/components/AmountPreview";
import { FormField, selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { normalizeAmount, subtractDecimal, toInputDecimal } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { buildTiers, type TierRow } from "@/lib/feeTiers";
import { formatDecimal, formatMoney, formatQty } from "@/lib/format";

const ASSET_CLASSES: AssetClass[] = ["us_equity", "eu_equity", "crypto", "forex"];
const SCOPE_LABEL: Record<FeeScope, string> = { global: "globale", asset_class: "classe di asset", portfolio: "portafoglio" };
const SIDE_LABEL: Record<FeeSide, string> = { both: "entrambi", buy: "acquisto", sell: "vendita" };

function tiersText(tiers: FeeTier[] | null): string {
  if (!tiers?.length) return "—";
  return tiers
    .map((t) => (t.up_to === null ? `oltre: ${formatQty(t.pct)}%` : `fino a ${formatDecimal(t.up_to, 0)}: ${formatQty(t.pct)}%`))
    .join("; ");
}

const optional = (v: string | null, render: (v: string) => string) => (v === null ? "—" : render(v));

interface FormState {
  name: string;
  scope: FeeScope;
  scope_ref: string;
  side: FeeSide;
  currency: string;
  fixed: string;
  pct: string;
  min_fee: string;
  max_fee: string;
  fx_pct: string;
  priority: string;
  active: boolean;
  tiers: TierRow[];
}

function initialForm(rule: FeeRule | null): FormState {
  if (!rule) {
    return {
      name: "", scope: "global", scope_ref: "", side: "both", currency: "EUR", fixed: "0", pct: "0",
      min_fee: "", max_fee: "", fx_pct: "0", priority: "0", active: true, tiers: [],
    };
  }
  return {
    name: rule.name,
    scope: rule.scope,
    scope_ref: rule.scope_ref ?? "",
    side: rule.side,
    currency: rule.currency,
    fixed: toInputDecimal(rule.fixed),
    pct: toInputDecimal(rule.pct),
    min_fee: toInputDecimal(rule.min_fee),
    max_fee: toInputDecimal(rule.max_fee),
    fx_pct: toInputDecimal(rule.fx_pct),
    priority: String(rule.priority),
    active: rule.active,
    tiers: (rule.tiers ?? []).map((t) => ({ up_to: toInputDecimal(t.up_to), pct: toInputDecimal(t.pct) })),
  };
}

const FORM_FIELDS = ["name", "scope", "scope_ref", "side", "currency", "fixed", "pct", "min_fee", "max_fee", "fx_pct", "priority", "tiers", "active"];

/** Mounted per opening: the form is initialised from `rule` (null = new rule). */
function RuleDialog({ rule, onClose }: { rule: FeeRule | null; onClose: () => void }) {
  const [form, setForm] = useState(() => initialForm(rule));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [tierErrors, setTierErrors] = useState<Record<number, string>>({});
  const portfolios = usePortfolios();
  const save = useSaveFeeRule();

  const set = <K extends keyof FormState>(field: K, value: FormState[K]) => setForm((f) => ({ ...f, [field]: value }));
  const text = (field: keyof FormState) => (e: { target: { value: string } }) => set(field, e.target.value as never);
  const setTier = (i: number, patch: Partial<TierRow>) =>
    set("tiers", form.tiers.map((t, j) => (j === i ? { ...t, ...patch } : t)));

  const serverField = errorField(save.error);
  const fieldError = (field: string) =>
    errors[field] ?? (save.error && serverField === field ? errorMessage(save.error) : null);
  const generalError = save.error && (!serverField || !FORM_FIELDS.includes(serverField)) ? errorMessage(save.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (!form.name.trim()) errs.name = "Il nome è obbligatorio";
    if (form.scope !== "global" && !form.scope_ref) errs.scope_ref = "Il riferimento è obbligatorio";
    if (!/^[A-Za-z0-9]{2,16}$/.test(form.currency.trim())) errs.currency = "Valuta non valida";
    const fixed = normalizeAmount(form.fixed || "0");
    const pct = normalizeAmount(form.pct || "0");
    const fx = normalizeAmount(form.fx_pct || "0");
    const min = form.min_fee.trim() ? normalizeAmount(form.min_fee) : null;
    const max = form.max_fee.trim() ? normalizeAmount(form.max_fee) : null;
    if (fixed === null) errs.fixed = "Importo non valido";
    if (pct === null) errs.pct = "Percentuale non valida";
    if (fx === null) errs.fx_pct = "Percentuale non valida";
    if (form.min_fee.trim() && min === null) errs.min_fee = "Importo non valido";
    if (form.max_fee.trim() && max === null) errs.max_fee = "Importo non valido";
    const gap = min !== null && max !== null ? subtractDecimal(min, max) : null;
    if (gap !== null && !gap.startsWith("-") && gap !== "0") errs.min_fee = "Il minimo non può superare il massimo";
    if (!/^-?\d+$/.test(form.priority.trim())) errs.priority = "Inserisci un intero";
    const { tiers, rowErrors } = buildTiers(form.tiers);
    setTierErrors(rowErrors);
    setErrors(errs);
    if (Object.keys(errs).length || Object.keys(rowErrors).length) return;

    const input: FeeRuleInput = {
      name: form.name.trim(),
      scope: form.scope,
      scope_ref: form.scope === "global" ? null : form.scope_ref,
      side: form.side,
      currency: form.currency.trim().toUpperCase(),
      fixed: fixed!,
      pct: pct!,
      min_fee: min,
      max_fee: max,
      tiers,
      fx_pct: fx!,
      priority: Number(form.priority.trim()),
      active: form.active,
    };
    save.mutate({ id: rule?.id ?? null, rule: input }, { onSuccess: onClose });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Regola di commissione</DialogTitle>
          <DialogDescription>Gli scaglioni sono in ordine crescente; l'ultimo vale "oltre".</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="sm:col-span-2">
            <FormField label="Nome" error={fieldError("name")}>
              <Input value={form.name} onChange={text("name")} autoFocus />
            </FormField>
          </div>
          <FormField label="Portata" error={fieldError("scope")}>
            <select
              className={selectClass}
              value={form.scope}
              onChange={(e) => setForm((f) => ({ ...f, scope: e.target.value as FeeScope, scope_ref: "" }))}
            >
              {(Object.keys(SCOPE_LABEL) as FeeScope[]).map((s) => (
                <option key={s} value={s}>
                  {SCOPE_LABEL[s]}
                </option>
              ))}
            </select>
          </FormField>
          {form.scope === "global" ? (
            <div />
          ) : (
            <FormField label="Riferimento" error={fieldError("scope_ref")}>
              <select className={selectClass} value={form.scope_ref} onChange={text("scope_ref")}>
                <option value="">Seleziona…</option>
                {form.scope === "asset_class"
                  ? ASSET_CLASSES.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))
                  : portfolios.data?.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
              </select>
            </FormField>
          )}
          <FormField label="Lato" error={fieldError("side")}>
            <select className={selectClass} value={form.side} onChange={text("side")}>
              {(Object.keys(SIDE_LABEL) as FeeSide[]).map((s) => (
                <option key={s} value={s}>
                  {SIDE_LABEL[s]}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Valuta" error={fieldError("currency")}>
            <Input value={form.currency} onChange={text("currency")} />
          </FormField>
          <FormField label="Commissione fissa" error={fieldError("fixed")}>
            <Input value={form.fixed} onChange={text("fixed")} inputMode="decimal" />
          </FormField>
          <FormField label="Commissione %" error={fieldError("pct")}>
            <Input value={form.pct} onChange={text("pct")} inputMode="decimal" />
          </FormField>
          <FormField label="Minimo" error={fieldError("min_fee")}>
            <Input value={form.min_fee} onChange={text("min_fee")} inputMode="decimal" />
          </FormField>
          <FormField label="Massimo" error={fieldError("max_fee")}>
            <Input value={form.max_fee} onChange={text("max_fee")} inputMode="decimal" />
          </FormField>
          <FormField label="Cambio %" error={fieldError("fx_pct")}>
            <Input value={form.fx_pct} onChange={text("fx_pct")} inputMode="decimal" />
          </FormField>
          <FormField label="Priorità" error={fieldError("priority")}>
            <Input value={form.priority} onChange={text("priority")} inputMode="numeric" />
          </FormField>

          <fieldset className="space-y-2 sm:col-span-2">
            <legend className="mb-1 text-sm font-medium">Scaglioni (% per controvalore)</legend>
            {form.tiers.map((t, i) => {
              const last = i === form.tiers.length - 1;
              return (
                <div key={i} className="flex items-start gap-2">
                  <div className="flex-1">
                    <Input
                      aria-label={`Limite scaglione ${i + 1}`}
                      value={last ? "oltre" : t.up_to}
                      disabled={last}
                      inputMode="decimal"
                      placeholder="fino a"
                      onChange={(e) => setTier(i, { up_to: e.target.value })}
                    />
                    {!last && (
                      <p className="mt-1 text-xs text-muted">
                        <AmountPreview input={t.up_to} currency={form.currency} />
                      </p>
                    )}
                  </div>
                  <div className="flex-1">
                    <Input
                      aria-label={`Percentuale scaglione ${i + 1}`}
                      value={t.pct}
                      inputMode="decimal"
                      placeholder="%"
                      onChange={(e) => setTier(i, { pct: e.target.value })}
                    />
                    <p className="mt-1 text-xs text-muted">
                      <AmountPreview input={t.pct} suffix="%" />
                    </p>
                    {tierErrors[i] && <p className="mt-1 text-xs text-loss">{tierErrors[i]}</p>}
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-label={`Rimuovi scaglione ${i + 1}`}
                    onClick={() => set("tiers", form.tiers.filter((_, j) => j !== i))}
                  >
                    Rimuovi
                  </Button>
                </div>
              );
            })}
            {fieldError("tiers") && <p className="text-xs text-loss">{fieldError("tiers")}</p>}
            <Button type="button" variant="outline" size="sm" onClick={() => set("tiers", [...form.tiers, { up_to: "", pct: "" }])}>
              <Plus />
              Aggiungi scaglione
            </Button>
          </fieldset>

          <div className="flex items-center gap-2 sm:col-span-2">
            <input
              id="fee-active"
              type="checkbox"
              checked={form.active}
              onChange={(e) => set("active", e.target.checked)}
              className="h-4 w-4 accent-[rgb(var(--accent))]"
            />
            <label htmlFor="fee-active">Attiva</label>
            {fieldError("active") && <span className="text-xs text-loss">{fieldError("active")}</span>}
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

function Simulator() {
  const portfolios = usePortfolios();
  const simulate = useSimulateFee();
  const [portfolioId, setPortfolioId] = useState("");
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [mode, setMode] = useState<"qty" | "notional">("qty");
  const [amount, setAmount] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(e: FormEvent) {
    e.preventDefault();
    const value = normalizeAmount(amount);
    if (!portfolioId || !symbol.trim()) return setError("Portafoglio e simbolo sono obbligatori");
    if (value === null) return setError("Importo non valido");
    setError(null);
    simulate.mutate({ portfolio_id: portfolioId, symbol: symbol.trim().toUpperCase(), side, [mode]: value });
  }

  const res = simulate.data;
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Simulatore</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-5" noValidate>
          <FormField label="Portafoglio">
            <select className={selectClass} value={portfolioId} onChange={(e) => setPortfolioId(e.target.value)}>
              <option value="">Seleziona…</option>
              {portfolios.data?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Simbolo">
            <Input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="AAPL" />
          </FormField>
          <FormField label="Lato">
            <select className={selectClass} value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
              <option value="buy">acquisto</option>
              <option value="sell">vendita</option>
            </select>
          </FormField>
          <FormField label="Indica">
            <select className={selectClass} value={mode} onChange={(e) => setMode(e.target.value as "qty" | "notional")}>
              <option value="qty">quantità</option>
              <option value="notional">controvalore</option>
            </select>
          </FormField>
          <FormField label={mode === "qty" ? "Quantità" : "Controvalore"}>
            <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" />
          </FormField>
          <div className="sm:col-span-5">
            <Button type="submit" disabled={simulate.isPending}>
              Simula
            </Button>
          </div>
        </form>
        {(error || simulate.error) && (
          <p role="alert" className="mt-3 text-sm text-loss">
            {error ?? errorMessage(simulate.error)}
          </p>
        )}
        {res && (
          <section aria-label="Risultato simulazione" className="mt-4 grid gap-1 text-sm">
            <div>
              Regola applicata: <strong>{res.rule_name}</strong>
            </div>
            {res.notional_eur != null && (
              <div>
                Controvalore: <Money value={res.notional_eur} />
              </div>
            )}
            <div>
              Commissione: <span className="num">{formatMoney(res.fee, res.fee_currency)}</span>
            </div>
            <div>
              Commissione di cambio: <Money value={res.fx_fee_eur} />
            </div>
          </section>
        )}
      </CardContent>
    </Card>
  );
}

export default function Fees() {
  const { data, isLoading, isError } = useFeeRules();
  const portfolios = usePortfolios({ includeArchived: true }); // names of every rule's portfolio
  const deactivate = useDeactivateFeeRule();
  const [editing, setEditing] = useState<FeeRule | "new" | null>(null);
  const refName = (r: FeeRule) =>
    r.scope === "portfolio" ? (portfolios.data?.find((p) => p.id === r.scope_ref)?.name ?? r.scope_ref) : r.scope_ref;

  const columns: Column<FeeRule>[] = [
    { key: "name", header: "Nome", sortValue: (r) => r.name, cell: (r) => r.name },
    { key: "scope", header: "Portata", cell: (r) => SCOPE_LABEL[r.scope] },
    { key: "ref", header: "Riferimento", cell: (r) => refName(r) ?? "—" },
    { key: "side", header: "Lato", cell: (r) => SIDE_LABEL[r.side] },
    {
      key: "fixed",
      header: "Fissa",
      align: "right",
      cell: (r) => <span className="num">{formatMoney(r.fixed, r.currency)}</span>,
    },
    { key: "pct", header: "%", align: "right", cell: (r) => <span className="num">{formatQty(r.pct)}%</span> },
    {
      key: "min",
      header: "Min",
      align: "right",
      cell: (r) => <span className="num">{optional(r.min_fee, (v) => formatMoney(v, r.currency))}</span>,
    },
    {
      key: "max",
      header: "Max",
      align: "right",
      cell: (r) => <span className="num">{optional(r.max_fee, (v) => formatMoney(v, r.currency))}</span>,
    },
    { key: "tiers", header: "Scaglioni", cell: (r) => tiersText(r.tiers) },
    { key: "fx", header: "Cambio", align: "right", cell: (r) => <span className="num">{formatQty(r.fx_pct)}%</span> },
    {
      key: "priority",
      header: "Priorità",
      align: "right",
      sortValue: (r) => r.priority,
      cell: (r) => <span className="num">{r.priority}</span>,
    },
    { key: "active", header: "Attiva", cell: (r) => (r.active ? "Sì" : "No") },
    {
      key: "actions",
      header: "Azioni",
      align: "right",
      cell: (r) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="outline" aria-label={`Modifica ${r.name}`} onClick={() => setEditing(r)}>
            <Pencil />
            Modifica
          </Button>
          {r.active && (
            <Button
              size="sm"
              variant="outline"
              aria-label={`Disattiva ${r.name}`}
              disabled={deactivate.isPending}
              onClick={() => deactivate.mutate(r.id)}
            >
              <PowerOff />
              Disattiva
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Commissioni">
        <Button onClick={() => setEditing("new")}>
          <Plus />
          Nuova regola
        </Button>
      </PageHeader>
      {isError && <p className="mb-4 text-loss">Impossibile caricare le regole</p>}
      {deactivate.error && (
        <p role="alert" className="mb-4 text-loss">
          {errorMessage(deactivate.error)}
        </p>
      )}
      <Card>
        <CardContent className="overflow-x-auto pt-6">
          <DataTable
            aria-label="Regole di commissione"
            columns={columns}
            rows={data}
            rowKey={(r) => r.id}
            loading={isLoading}
            empty="Nessuna regola"
          />
        </CardContent>
      </Card>
      <Simulator />
      {editing && <RuleDialog rule={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
