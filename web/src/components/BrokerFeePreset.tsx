import { useState } from "react";
import { toast } from "sonner";
import { useApplyFeePreset, useBrokerFeePreset, useFeeRules } from "@/api/hooks/fees";
import type { BrokerFeePreset, Portfolio } from "@/api/types";
import { AdminOnly } from "@/components/AdminOnly";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { entryLabel, feeSummary, presetRuleName } from "@/lib/feePreset";
import { assetClassLabel } from "@/lib/labels";

/** Each rule of a preset: what it covers, its fees and how sure the figures are. */
export function PresetEntries({ preset }: { preset: BrokerFeePreset }) {
  return (
    <ul aria-label={`Commissioni di ${preset.broker}`} className="space-y-0.5 text-xs text-muted">
      {preset.entries.map((e) => (
        <li key={e.asset_class ?? "all"}>
          {entryLabel(e, preset)}: {feeSummary(e)} ({e.source_note})
        </li>
      ))}
    </ul>
  );
}

/**
 * The «Usa le commissioni di <broker>» checkbox of a link flow (checked by default): the
 * admin confirms the broker's preset or keeps the portfolio's current fee rule. Nothing
 * for a broker without a preset.
 */
export function FeePresetCheckbox({
  preset,
  checked,
  onChange,
}: {
  preset: BrokerFeePreset | undefined;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  if (!preset) return null;
  return (
    <label className="flex items-start gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 accent-[rgb(var(--accent))]"
      />
      <span>
        Usa le commissioni di {preset.broker}
        <PresetEntries preset={preset} />
        <span className="block text-xs text-muted">Se togli la spunta restano le regole attuali del portafoglio.</span>
      </span>
    </label>
  );
}

/**
 * «Commissioni» of a portfolio: its own (portfolio-scope) active rules and, for a portfolio
 * linked to a broker with a preset, «Usa le commissioni di <broker>» (confirmed).
 */
export function PortfolioFees({ portfolio }: { portfolio: Portfolio }) {
  const rules = useFeeRules();
  const preset = useBrokerFeePreset(portfolio.broker_kind);
  const apply = useApplyFeePreset();
  const [open, setOpen] = useState(false);
  const own = (rules.data ?? []).filter((r) => r.scope === "portfolio" && r.scope_ref === portfolio.id && r.active);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Commissioni</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {rules.isError ? (
          <p className="text-loss">Impossibile caricare le regole</p>
        ) : own.length === 0 ? (
          <p className="text-muted">Nessuna regola del portafoglio: valgono le regole generali (pagina Commissioni).</p>
        ) : (
          <ul aria-label="Regole del portafoglio" className="space-y-1">
            {own.map((r) => (
              <li key={r.id}>
                <span className="font-medium">{r.name}</span>{" "}
                <span className="text-muted">
                  · {r.asset_class ? `classe ${assetClassLabel(r.asset_class)}` : "tutte le classi"} · {feeSummary(r)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {preset && (
          <AdminOnly>
            <div className="space-y-1">
              <Button type="button" size="sm" variant="outline" onClick={() => { apply.reset(); setOpen(true); }}>
                Usa le commissioni di {preset.broker}
              </Button>
              <PresetEntries preset={preset} />
            </div>
            <ConfirmDialog
              open={open}
              onOpenChange={setOpen}
              title={`Usare le commissioni di ${preset.broker}?`}
              description={`${preset.entries.map((e) => `«${presetRuleName(portfolio.name, preset, e)}»`).join(", ")} ${preset.entries.length > 1 ? "diventano le regole" : "diventa la regola"} del portafoglio; le altre regole del portafoglio vengono disattivate, non cancellate.`}
              confirmLabel="Applica"
              pending={apply.isPending}
              error={apply.error ? errorMessage(apply.error) : null}
              onConfirm={() =>
                apply.mutate(
                  { portfolioId: portfolio.id, brokerKind: preset.broker_kind },
                  {
                    onSuccess: () => {
                      toast.success(`Commissioni di ${preset.broker} applicate`);
                      setOpen(false);
                    },
                  },
                )
              }
            >
              <PresetEntries preset={preset} />
            </ConfirmDialog>
          </AdminOnly>
        )}
      </CardContent>
    </Card>
  );
}
