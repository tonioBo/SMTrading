import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { usePatchPortfolio, useRiskProfiles } from "@/api/hooks/portfolios";
import type { Portfolio } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { NameWarning } from "@/components/NameWarning";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorField, errorMessage } from "@/lib/errors";
import { buildPortfolioPatch, initialForm, PORTFOLIO_FIELDS as FIELDS, type PortfolioField as Field, type PortfolioForm as Form } from "@/lib/portfolioPatch";
import { timezones } from "@/lib/timezones";

function SettingsForm({
  portfolio,
  nameWarning,
  onSaved,
}: {
  portfolio: Portfolio;
  nameWarning: string | null;
  onSaved: (saved: Portfolio) => void;
}) {
  const [form, setForm] = useState<Form>(() => initialForm(portfolio));
  const [errors, setErrors] = useState<Partial<Record<Field, string>>>({});
  const patch = usePatchPortfolio(portfolio.id);
  const profiles = useRiskProfiles();
  const selectedProfile = profiles.data?.find((p) => p.id === form.risk_profile_id);
  const serverField = errorField(patch.error) as Field | null;
  const fieldError = (f: Field) => errors[f] ?? (patch.error && serverField === f ? errorMessage(patch.error) : null);
  const generalError = patch.error && !(serverField && FIELDS.includes(serverField)) ? errorMessage(patch.error) : null;
  const set = (f: Field) => (e: { target: { value: string } }) => setForm((cur) => ({ ...cur, [f]: e.target.value }));
  const { patch: body } = buildPortfolioPatch(portfolio, form);
  const dirty = Object.keys(body).length > 0;

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const built = buildPortfolioPatch(portfolio, form);
    setErrors(built.errors);
    if (Object.keys(built.errors).length > 0 || Object.keys(built.patch).length === 0) return;
    patch.mutate(built.patch, {
      onSuccess: (saved) => {
        onSaved(saved);
        toast.success("Impostazioni salvate");
      },
    });
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-2" noValidate aria-label="Impostazioni del portafoglio">
      <div className="space-y-1.5">
        <FormField label="Nome" error={fieldError("name")}>
          <Input value={form.name} onChange={set("name")} maxLength={128} />
        </FormField>
        <NameWarning warning={nameWarning} />
      </div>
      <FormField label="Leva" error={fieldError("leverage")} hint="da 1 a 10">
        <Input value={form.leverage} onChange={set("leverage")} inputMode="decimal" />
      </FormField>
      <FormField label="Margine di mantenimento (%)" error={fieldError("maintenance_margin_pct")}>
        <Input value={form.maintenance_margin_pct} onChange={set("maintenance_margin_pct")} inputMode="decimal" />
      </FormField>
      <FormField label="Soglia di avviso (%)" error={fieldError("warning_margin_pct")} hint="maggiore del mantenimento, al massimo 100">
        <Input value={form.warning_margin_pct} onChange={set("warning_margin_pct")} inputMode="decimal" />
      </FormField>
      <FormField label="Slippage (bps)" error={fieldError("slippage_bps")}>
        <Input value={form.slippage_bps} onChange={set("slippage_bps")} inputMode="numeric" />
      </FormField>
      <div>
      <FormField label="Profilo di rischio" error={fieldError("risk_profile_id")}>
        <select className={selectClass} value={form.risk_profile_id} onChange={set("risk_profile_id")}>
          <option value="">Nessuno</option>
          {profiles.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </FormField>
        {selectedProfile?.description && (
          <p data-testid="risk-profile-description" className="mt-1.5 text-xs text-muted">
            {selectedProfile.description}
          </p>
        )}
      </div>
      <FormField label="Fuso orario" error={fieldError("timezone")}>
        <select className={selectClass} value={form.timezone} onChange={set("timezone")}>
          {timezones(portfolio.timezone).map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
      </FormField>
      {generalError && (
        <p role="alert" className="text-sm text-loss sm:col-span-2">
          {generalError}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" disabled={patch.isPending || !dirty}>
          Salva impostazioni
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={patch.isPending || !dirty}
          onClick={() => {
            setForm(initialForm(portfolio));
            setErrors({});
            patch.reset();
          }}
        >
          Annulla modifiche
        </Button>
      </div>
    </form>
  );
}

export function PortfolioSettings({ portfolio }: { portfolio: Portfolio }) {
  // remount with the saved values once the portfolio is refetched after a save
  const key = FIELDS.map((f) => initialForm(portfolio)[f]).join("\u0000");
  // kept across that remount: the warning of the last save
  const [nameWarning, setNameWarning] = useState<string | null>(null);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Impostazioni del portafoglio</CardTitle>
      </CardHeader>
      <CardContent>
        <SettingsForm
          key={key}
          portfolio={portfolio}
          nameWarning={nameWarning}
          onSaved={(saved) => setNameWarning(saved.name_warning ?? null)}
        />
      </CardContent>
    </Card>
  );
}
