import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useAgents } from "@/api/hooks/agents";
import { usePatchPortfolio } from "@/api/hooks/portfolios";
import type { Portfolio, PortfolioPatch } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorField, errorMessage } from "@/lib/errors";

const TIMEFRAMES = ["5m", "15m", "1h"] as const;
const NO_AGENT_LABEL = "nessuno: solo registrati e avviso";
const HELP = "Spenti: il portafoglio lavora come oggi. In giornata la leva è al massimo 2 sui conti di prova e 1 sui conti reali.";
const ONE_MODE = "Un portafoglio lavora in un solo modo: spegni prima l'altro interruttore.";

type Field = "intraday_timeframes" | "intraday_wake_agent_id" | "day_close_minutes" | "max_day_trades_5d" | "intraday_max_wakes_per_hour";

interface Form {
  timeframes: string[];
  agent: string;
  closeMinutes: string;
  cap: string;
  wakes: string;
}

const initial = (p: Portfolio): Form => ({
  timeframes: p.intraday_timeframes ?? ["15m", "1h"],
  agent: p.intraday_wake_agent_id ?? "",
  closeMinutes: String(p.day_close_minutes ?? 10),
  cap: p.max_day_trades_5d == null ? "" : String(p.max_day_trades_5d),
  wakes: String(p.intraday_max_wakes_per_hour ?? 6),
});

/** An integer typed as digits within [lo, hi], else the message for its field. */
function integer(text: string, label: string, lo: number, hi: number): { value: number } | { error: string } {
  const t = text.trim();
  const n = /^\d+$/.test(t) ? Number(t) : NaN;
  return n >= lo && n <= hi ? { value: n } : { error: `${label}: un numero intero da ${lo} a ${hi}` };
}

/** The PATCH body with only the changed fields, or the errors of the invalid ones. */
function buildPatch(p: Portfolio, f: Form): { patch: PortfolioPatch; errors: Partial<Record<Field, string>> } {
  const start = initial(p);
  const patch: PortfolioPatch = {};
  const errors: Partial<Record<Field, string>> = {};
  const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);
  if (!same(f.timeframes, start.timeframes)) {
    if (f.timeframes.length === 0) errors.intraday_timeframes = "Scegli almeno un intervallo";
    else patch.intraday_timeframes = f.timeframes;
  }
  if (f.agent !== start.agent) patch.intraday_wake_agent_id = f.agent || null;
  if (f.closeMinutes.trim() !== start.closeMinutes) {
    const r = integer(f.closeMinutes, "Minuti prima della fine", 2, 60);
    if ("error" in r) errors.day_close_minutes = r.error;
    else patch.day_close_minutes = r.value;
  }
  if (f.cap.trim() !== start.cap) {
    if (f.cap.trim() === "") patch.max_day_trades_5d = null;
    else {
      const r = integer(f.cap, "Tetto", 0, 100);
      if ("error" in r) errors.max_day_trades_5d = r.error;
      else patch.max_day_trades_5d = r.value;
    }
  }
  if (f.wakes.trim() !== start.wakes) {
    const r = integer(f.wakes, "Sveglie all'ora", 1, 60);
    if ("error" in r) errors.intraday_max_wakes_per_hour = r.error;
    else patch.intraday_max_wakes_per_hour = r.value;
  }
  return { patch, errors };
}

/** A switch saved on change: the server's refusal is shown under it and it stays as it was. */
function ModeSwitch({
  portfolio,
  field,
  label,
  blockedBy,
}: {
  portfolio: Portfolio;
  field: "intraday_enabled" | "auto_rules_enabled";
  label: string;
  /** the other mode is on: this one is disabled with the reason */
  blockedBy: boolean;
}) {
  const patch = usePatchPortfolio(portfolio.id);
  const qc = useQueryClient();
  const checked = portfolio[field] === true;
  return (
    <div className="space-y-1">
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={checked}
          disabled={patch.isPending || (blockedBy && !checked)}
          onChange={(e) => {
            // read now: the controlled checkbox is reset before the PATCH answers
            const next = e.target.checked;
            patch.reset();
            patch.mutate(
              { [field]: next },
              {
                onSuccess: () => {
                  void qc.invalidateQueries({ queryKey: ["intraday"] });
                  toast.success(next ? `${label}: acceso` : `${label}: spento`);
                },
              },
            );
          }}
          className="h-4 w-4 accent-[rgb(var(--accent))]"
        />
        {label}
      </label>
      {blockedBy && !checked && <p className="text-xs text-muted">{ONE_MODE}</p>}
      {patch.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(patch.error)}
        </p>
      )}
    </div>
  );
}

function DayForm({ portfolio }: { portfolio: Portfolio }) {
  const agents = useAgents();
  const patch = usePatchPortfolio(portfolio.id);
  const qc = useQueryClient();
  const [form, setForm] = useState<Form>(() => initial(portfolio));
  const [errors, setErrors] = useState<Partial<Record<Field, string>>>({});
  const wakeAgents = (agents.data ?? []).filter((a) => a.active && a.portfolios.some((l) => l.portfolio_id === portfolio.id && l.permission === "trade"));
  // the saved agent stays selectable even if it lost the permission: the server will say so
  const savedAgent = portfolio.intraday_wake_agent_id;
  const serverField = errorField(patch.error) as Field | null;
  const fieldError = (f: Field) => errors[f] ?? (patch.error && serverField === f ? errorMessage(patch.error) : null);
  const generalError = patch.error && !serverField ? errorMessage(patch.error) : null;
  const { patch: body, errors: invalid } = buildPatch(portfolio, form);
  const dirty = Object.keys(body).length > 0 || Object.keys(invalid).length > 0;

  function submit(e: FormEvent) {
    e.preventDefault();
    const built = buildPatch(portfolio, form);
    setErrors(built.errors);
    if (Object.keys(built.errors).length > 0 || Object.keys(built.patch).length === 0) return;
    patch.mutate(built.patch, {
      onSuccess: () => {
        void qc.invalidateQueries({ queryKey: ["intraday"] });
        toast.success("Lavoro in giornata salvato");
      },
    });
  }

  return (
    <form onSubmit={submit} noValidate aria-label="Lavoro in giornata" className="grid gap-4 sm:grid-cols-2">
      <fieldset className="space-y-1.5 sm:col-span-2">
        <legend className="mb-1 text-sm font-medium">Intervalli osservati</legend>
        <div className="flex flex-wrap gap-4">
          {TIMEFRAMES.map((tf) => (
            <label key={tf} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={form.timeframes.includes(tf)}
                onChange={(e) =>
                  setForm((cur) => ({ ...cur, timeframes: TIMEFRAMES.filter((x) => (x === tf ? e.target.checked : cur.timeframes.includes(x))) }))
                }
                className="h-4 w-4 accent-[rgb(var(--accent))]"
              />
              {tf}
            </label>
          ))}
        </div>
        {fieldError("intraday_timeframes") && <p className="text-xs text-loss">{fieldError("intraday_timeframes")}</p>}
      </fieldset>
      <FormField label="Agente da svegliare" error={fieldError("intraday_wake_agent_id")}>
        <select className={selectClass} value={form.agent} onChange={(e) => setForm((c) => ({ ...c, agent: e.target.value }))}>
          <option value="">{NO_AGENT_LABEL}</option>
          {wakeAgents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
          {savedAgent && !wakeAgents.some((a) => a.id === savedAgent) && <option value={savedAgent}>agente non più abilitato</option>}
        </select>
      </FormField>
      <FormField label="Chiusura di sicurezza (minuti prima della fine)" error={fieldError("day_close_minutes")} hint="da 2 a 60">
        <Input value={form.closeMinutes} inputMode="numeric" onChange={(e) => setForm((c) => ({ ...c, closeMinutes: e.target.value }))} />
      </FormField>
      <FormField label="Tetto operazioni di giornata su 5 sedute" error={fieldError("max_day_trades_5d")} hint="vuoto = nessun tetto">
        <Input value={form.cap} inputMode="numeric" onChange={(e) => setForm((c) => ({ ...c, cap: e.target.value }))} />
      </FormField>
      <FormField label="Sveglie massime all'ora" error={fieldError("intraday_max_wakes_per_hour")} hint="da 1 a 60">
        <Input value={form.wakes} inputMode="numeric" onChange={(e) => setForm((c) => ({ ...c, wakes: e.target.value }))} />
      </FormField>
      {generalError && (
        <p role="alert" className="text-sm text-loss sm:col-span-2">
          {generalError}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" disabled={patch.isPending || !dirty}>
          Salva lavoro in giornata
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={patch.isPending || !dirty}
          onClick={() => {
            setForm(initial(portfolio));
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

/** «Lavoro in giornata»: the two mode switches (one mode per portfolio) and the intraday
 * settings. Mount it inside AdminOnly: a viewer does not see it. */
export function IntradaySettings({ portfolio }: { portfolio: Portfolio }) {
  const modeA = portfolio.intraday_enabled === true;
  const modeB = portfolio.auto_rules_enabled === true;
  // remount with the saved values once the portfolio is refetched after a save
  const key = JSON.stringify(initial(portfolio));
  return (
    <section aria-labelledby="intraday-title" className="mt-4 space-y-4 border-t border-border pt-4">
      <div>
        <h3 id="intraday-title" className="font-medium">
          Lavoro in giornata
        </h3>
        <p className="text-xs text-muted">{HELP}</p>
      </div>
      <ModeSwitch portfolio={portfolio} field="intraday_enabled" label="Sveglia dell'agente (modo A)" blockedBy={modeB} />
      <ModeSwitch portfolio={portfolio} field="auto_rules_enabled" label="Regole automatiche (modo B, prove)" blockedBy={modeA} />
      <DayForm key={key} portfolio={portfolio} />
    </section>
  );
}
