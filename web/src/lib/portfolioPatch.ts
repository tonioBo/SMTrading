import type { Portfolio, PortfolioPatch } from "@/api/types";
import { parseAmount, toInputDecimal } from "@/lib/decimal";
import { CODE_ERROR, CODE_RE } from "@/lib/portfolioCode";

export type PortfolioForm = {
  name: string;
  code: string;
  leverage: string;
  maintenance_margin_pct: string;
  warning_margin_pct: string;
  slippage_bps: string;
  risk_profile_id: string;
  timezone: string;
};
export type PortfolioField = keyof PortfolioForm;

export const PORTFOLIO_FIELDS: PortfolioField[] = ["name", "code", "leverage", "maintenance_margin_pct", "warning_margin_pct", "slippage_bps", "risk_profile_id", "timezone"];

export function initialForm(p: Portfolio): PortfolioForm {
  return {
    name: p.name,
    code: p.code ?? "",
    leverage: toInputDecimal(p.leverage),
    maintenance_margin_pct: toInputDecimal(p.maintenance_margin_pct),
    warning_margin_pct: toInputDecimal(p.warning_margin_pct),
    slippage_bps: String(p.slippage_bps),
    risk_profile_id: p.risk_profile_id ?? "",
    timezone: p.timezone,
  };
}

/** A decimal typed in it-IT (R10 rules) → plain API string, or the error to show. */
function decimalField(text: string, label: string, min: number, max: number): { value?: string; error?: string } {
  const parsed = parseAmount(text);
  if (!parsed.ok) return { error: parsed.error === "Importo non valido" ? `${label} non valida` : parsed.error };
  const n = Number(parsed.value); // range check only: the value sent is the exact string
  if (n < min || n > max) return { error: `${label}: da ${String(min).replace(".", ",")} a ${max}` };
  return { value: parsed.value };
}

/** The PATCH body with only the changed fields, or the per-field validation errors. */
export function buildPortfolioPatch(p: Portfolio, form: PortfolioForm): { patch: PortfolioPatch; errors: Partial<Record<PortfolioField, string>> } {
  const initial = initialForm(p);
  const patch: PortfolioPatch = {};
  const errors: Partial<Record<PortfolioField, string>> = {};
  const changed = (f: PortfolioField) => form[f].trim() !== initial[f].trim();

  if (changed("name")) {
    if (!form.name.trim()) errors.name = "Campo obbligatorio";
    else patch.name = form.name.trim();
  }
  const code = form.code.trim().toUpperCase();
  if (code !== initial.code) {
    if (code && !CODE_RE.test(code)) errors.code = CODE_ERROR;
    else patch.code = code || null;
  }
  const decimals: [PortfolioField & keyof PortfolioPatch, string, number, number][] = [
    ["leverage", "Leva", 1, 10],
    ["maintenance_margin_pct", "Margine di mantenimento", 0.01, 100],
    ["warning_margin_pct", "Soglia di avviso", 0.01, 100],
  ];
  for (const [field, label, min, max] of decimals) {
    if (!changed(field)) continue;
    const { value, error } = decimalField(form[field], label, min, max);
    if (error) errors[field] = error;
    else (patch as Record<string, string>)[field] = value!;
  }
  if (changed("slippage_bps")) {
    const text = form.slippage_bps.trim();
    if (!/^\d+$/.test(text) || Number(text) > 10_000) errors.slippage_bps = "Inserisci un intero da 0 a 10000 (bps)";
    else patch.slippage_bps = Number(text);
  }
  if (form.risk_profile_id !== initial.risk_profile_id) patch.risk_profile_id = form.risk_profile_id || null;
  if (form.timezone !== initial.timezone) patch.timezone = form.timezone;
  return { patch, errors };
}
