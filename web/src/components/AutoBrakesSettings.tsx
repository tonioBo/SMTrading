import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { usePatchPortfolio } from "@/api/hooks/portfolios";
import type { AutoValue, Portfolio } from "@/api/types";
import { AutoBrakeFields } from "@/components/AutoBrakeFields";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errors";
import { PORTFOLIO_BRAKES, PORTFOLIO_BRAKE_DEFAULTS, brakesToForm, parseBrakes } from "@/lib/autoRules";

function BrakesForm({ portfolio }: { portfolio: Portfolio }) {
  const patch = usePatchPortfolio(portfolio.id);
  const initial = () => brakesToForm(portfolio.auto_brakes as Record<string, AutoValue> | undefined, PORTFOLIO_BRAKE_DEFAULTS);
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});

  function submit(e: FormEvent) {
    e.preventDefault();
    const parsed = parseBrakes(values, PORTFOLIO_BRAKES);
    setErrors(parsed.errors);
    if (Object.keys(parsed.errors).length > 0) return;
    patch.mutate({ auto_brakes: parsed.brakes }, { onSuccess: () => toast.success("Freni delle regole automatiche salvati") });
  }

  return (
    <form onSubmit={submit} noValidate aria-label="Freni delle regole automatiche" className="space-y-3">
      <AutoBrakeFields specs={PORTFOLIO_BRAKES} values={values} errors={errors} onChange={(k, v) => setValues((c) => ({ ...c, [k]: v }))} />
      {patch.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(patch.error)}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={patch.isPending}>
          Salva i freni
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={patch.isPending}
          onClick={() => {
            setValues(initial());
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

/** «Freni delle regole automatiche»: the caps over every rule of the portfolio (shown with the
 * switch on). Mount it inside AdminOnly. */
export function AutoBrakesSettings({ portfolio }: { portfolio: Portfolio }) {
  if (portfolio.auto_rules_enabled !== true) return null;
  // remount with the saved values once the portfolio is refetched after a save
  const key = JSON.stringify(portfolio.auto_brakes ?? null);
  return (
    <section aria-labelledby="auto-brakes-title" className="mt-4 space-y-3 border-t border-border pt-4">
      <div>
        <h3 id="auto-brakes-title" className="font-medium">
          Freni delle regole automatiche
        </h3>
        <p className="text-xs text-muted">Limiti sull'insieme delle regole del portafoglio, nel giorno locale del portafoglio. Quando scattano, le regole si fermano e si riattivano a mano.</p>
      </div>
      <BrakesForm key={key} portfolio={portfolio} />
    </section>
  );
}
