import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useCreateAutoRule, useUpdateAutoRule } from "@/api/hooks/autoRules";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { AutoRule, ModelParam, ModelSpec } from "@/api/types";
import { AutoBrakeFields } from "@/components/AutoBrakeFields";
import { FormField, selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";
import { CHOICE_LABELS, MODEL_LABELS, PARAM_LABELS, RULE_BRAKES, RULE_BRAKE_DEFAULTS, brakesToForm, buildRuleBody, paramsToForm, type RuleFormState } from "@/lib/autoRules";

function ParamField({ p, value, error, onChange }: { p: ModelParam; value: string; error?: string; onChange: (v: string) => void }) {
  const meta = PARAM_LABELS[p.name] ?? { label: p.name, help: "" };
  const range = p.kind === "choice" ? "" : ` (da ${String(p.lo ?? "").replace(".", ",")} a ${String(p.hi ?? "").replace(".", ",")})`;
  return (
    <FormField label={meta.label} error={error} hint={`${meta.help}${range}`}>
      {p.kind === "choice" ? (
        <select className={selectClass} value={value} onChange={(e) => onChange(e.target.value)}>
          {p.choices.map((c) => (
            <option key={c} value={c}>
              {CHOICE_LABELS[c] ?? c}
            </option>
          ))}
        </select>
      ) : (
        <Input type="number" step="any" value={value} min={p.lo ?? undefined} max={p.hi ?? undefined} onChange={(e) => onChange(e.target.value)} />
      )}
    </FormField>
  );
}

/** The rule form: new (portfolio, symbol, model) or a new version of a rule that is not active. */
export function AutoRuleForm({ models, rule, onDone }: { models: ModelSpec[]; rule?: AutoRule; onDone: () => void }) {
  const portfolios = usePortfolios();
  const create = useCreateAutoRule();
  const update = useUpdateAutoRule(rule?.id ?? "");
  const pending = create.isPending || update.isPending;
  const mutationError = create.error ?? update.error;
  const firstModel = rule?.model ?? models[0]?.name ?? "";
  const [form, setForm] = useState<RuleFormState>(() => ({
    portfolioId: rule?.portfolio_id ?? "",
    symbol: rule?.symbol ?? "",
    model: firstModel,
    params: paramsToForm(models.find((m) => m.name === firstModel), rule?.params),
    size: rule?.notional != null ? "notional" : "qty",
    amount: rule ? (rule.notional ?? rule.qty ?? "") : "",
    brakes: brakesToForm(rule?.brakes, RULE_BRAKE_DEFAULTS),
  }));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const spec = models.find((m) => m.name === form.model);
  const set = <K extends keyof RuleFormState>(k: K, v: RuleFormState[K]) => setForm((c) => ({ ...c, [k]: v }));

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!spec) return;
    const found: Record<string, string> = {};
    if (!rule && form.portfolioId === "") found.portfolioId = "Scegli il portafoglio";
    if (!rule && form.symbol.trim() === "") found.symbol = "Scrivi il simbolo";
    const built = buildRuleBody(spec, form);
    const all = { ...found, ...built.errors };
    setErrors(all);
    if (Object.keys(all).length > 0) return;
    const done = () => {
      toast.success(rule ? "Regola salvata: nuova versione" : "Regola creata come bozza");
      onDone();
    };
    if (rule) update.mutate(built.body, { onSuccess: done });
    else create.mutate({ ...built.body, portfolio_id: form.portfolioId, symbol: form.symbol.trim(), model: form.model }, { onSuccess: done });
  }

  const own = spec?.params.filter((p) => !p.common) ?? [];
  const common = spec?.params.filter((p) => p.common) ?? [];
  const field = (p: ModelParam) => (
    <ParamField key={p.name} p={p} value={form.params[p.name] ?? ""} error={errors[`params.${p.name}`]} onChange={(v) => setForm((c) => ({ ...c, params: { ...c.params, [p.name]: v } }))} />
  );

  return (
    <form onSubmit={submit} noValidate aria-label={rule ? "Modifica della regola" : "Nuova regola"} className="space-y-5 rounded-md border border-border p-4">
      <h3 className="font-medium">{rule ? `Modifica: ${rule.symbol} (${MODEL_LABELS[rule.model] ?? rule.model})` : "Nuova regola"}</h3>
      {!rule && (
        <div className="grid gap-3 sm:grid-cols-3">
          <FormField label="Portafoglio" error={errors.portfolioId}>
            <select className={selectClass} value={form.portfolioId} onChange={(e) => set("portfolioId", e.target.value)}>
              <option value="">scegli…</option>
              {(portfolios.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Simbolo" error={errors.symbol} hint="azioni USA o cripto">
            <Input value={form.symbol} autoCapitalize="characters" onChange={(e) => set("symbol", e.target.value)} />
          </FormField>
          <FormField label="Modello">
            <select
              className={selectClass}
              value={form.model}
              onChange={(e) => {
                const next = models.find((m) => m.name === e.target.value);
                setForm((c) => ({ ...c, model: e.target.value, params: paramsToForm(next) }));
                setErrors({});
              }}
            >
              {models.map((m) => (
                <option key={m.name} value={m.name}>
                  {MODEL_LABELS[m.name] ?? m.name}
                </option>
              ))}
            </select>
          </FormField>
        </div>
      )}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Parametri del modello</legend>
        <div className="grid gap-3 sm:grid-cols-2">{own.map(field)}</div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Uscite, filtri e tipo di entrata</legend>
        <div className="grid gap-3 sm:grid-cols-2">{common.map(field)}</div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Quantità</legend>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="radio" name="size" checked={form.size === "qty"} onChange={() => set("size", "qty")} className="accent-[rgb(var(--accent))]" />
            Pezzi
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="size" checked={form.size === "notional"} onChange={() => set("size", "notional")} className="accent-[rgb(var(--accent))]" />
            Importo
          </label>
        </div>
        <FormField label={form.size === "qty" ? "Numero di pezzi" : "Importo per operazione"} error={errors.amount} hint="Pezzi o importo, uno solo.">
          <Input value={form.amount} inputMode="decimal" onChange={(e) => set("amount", e.target.value)} />
        </FormField>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Freni della regola</legend>
        <AutoBrakeFields
          specs={RULE_BRAKES}
          values={form.brakes}
          errors={Object.fromEntries(RULE_BRAKES.map((b) => [b.key, errors[`brakes.${b.key}`]]).filter(([, m]) => m))}
          onChange={(k, v) => setForm((c) => ({ ...c, brakes: { ...c.brakes, [k]: v } }))}
        />
      </fieldset>
      {mutationError && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(mutationError)}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={pending || !spec}>
          {rule ? "Salva come nuova versione" : "Crea la regola"}
        </Button>
        <Button type="button" variant="outline" disabled={pending} onClick={onDone}>
          Annulla
        </Button>
      </div>
    </form>
  );
}
