import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { useBrokerLinks } from "@/api/hooks/brokers";
import { useCampaignTemplates, useCreateCampaign } from "@/api/hooks/campaigns";
import { usePortfolios, useRiskProfiles } from "@/api/hooks/portfolios";
import type { BrokerLink, Campaign, CampaignGoalSpec, CampaignPortfolioSpec, CampaignTemplate } from "@/api/types";
import { GuidedTest } from "@/components/BrokerGuidedTest";
import { FormField, selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { BROKER_KINDS, brokerKindLabel } from "@/lib/broker";
import { addDays, formatDay, romeToday } from "@/lib/campaigns";
import { amountError, normalizeAmount } from "@/lib/decimal";
import { errorMessage } from "@/lib/errors";
import { formatDecimal } from "@/lib/format";

const STEPS = ["Nome e date", "Portafogli", "Obiettivi", "Riepilogo"];
const PAPER_KINDS = new Set<string>(BROKER_KINDS.map((k) => k.value));

interface NewPortfolio {
  key: string;
  name: string;
  initial_cash: string;
  base_currency: string;
  risk_profile_id: string;
  broker_link_id: string;
}

interface GoalChoice {
  on: boolean;
  target: string;
  plan: boolean;
}

/** Only paper/demo links, enabled: a real-money link never appears in the wizard. */
function testLinks(links: BrokerLink[] | undefined): BrokerLink[] {
  return (links ?? []).filter(
    (l) => PAPER_KINDS.has(l.kind) && l.enabled && !(l as BrokerLink & { live_state?: string | null }).live_state,
  );
}

function LinkPicker({
  label,
  value,
  links,
  onChange,
}: {
  label: string;
  value: string;
  links: BrokerLink[];
  onChange: (id: string) => void;
}) {
  const link = links.find((l) => l.id === value);
  return (
    <div className="space-y-2 sm:col-span-2">
      <FormField label={label} hint="Solo conti paper/demo: nessun denaro reale">
        <select className={selectClass} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Nessuno (simulato da SMTrading)</option>
          {links.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name} · {brokerKindLabel(l.kind)}
            </option>
          ))}
        </select>
      </FormField>
      {link && <GuidedTest link={link} />}
    </div>
  );
}

function Stepper({ step }: { step: number }) {
  return (
    <ol aria-label="Passi della procedura" className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
      {STEPS.map((label, i) => (
        <li
          key={label}
          aria-current={i + 1 === step ? "step" : undefined}
          className={i + 1 === step ? "font-semibold text-accent" : i + 1 < step ? "text-fg" : "text-muted"}
        >
          {i + 1}. {label}
        </li>
      ))}
    </ol>
  );
}

function targetLabel(t: CampaignTemplate, target: string): string {
  const pct = formatDecimal(normalizeAmount(target) ?? t.body.target_pct, 2);
  if (t.body.kind === "max_loss") return `−${pct}%`;
  if (t.body.kind === "beat_index") return `+${pct} pp su ${t.body.benchmark ?? ""}`;
  return `${pct}%`;
}

/** «Nuova campagna guidata»: name and dates, portfolios (new or existing, optionally on a
 * paper/demo account), goals from the templates, summary and confirm (one transaction). */
export function CampaignWizard({ onClose, onCreated }: { onClose: () => void; onCreated?: (c: Campaign) => void }) {
  const today = romeToday();
  const [step, setStep] = useState(1);
  const [header, setHeader] = useState({ name: "", start_date: today, end_date: addDays(today, 27), notes: "" });
  const [fresh, setFresh] = useState<NewPortfolio[]>([]);
  const [existing, setExisting] = useState<Record<string, string>>({}); // portfolio id → link id ("" = none)
  const [choices, setChoices] = useState<Record<string, GoalChoice>>({});
  const [templates, setTemplates] = useState<CampaignTemplate[] | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [counter, setCounter] = useState(1);
  const portfolios = usePortfolios();
  const profiles = useRiskProfiles();
  const links = testLinks(useBrokerLinks().data);
  const loadTemplates = useCampaignTemplates();
  const create = useCreateCampaign();

  const candidates = (portfolios.data ?? []).filter((p) => p.mode !== "reale" && p.status !== "archived");
  const names: Record<string, string> = {
    ...Object.fromEntries(candidates.map((p) => [p.id, p.name])),
    ...Object.fromEntries(fresh.map((p) => [p.key, p.name.trim() || "nuovo portafoglio"])),
  };

  function specs(): CampaignPortfolioSpec[] {
    return [
      ...fresh.map((p) => ({
        key: p.key,
        name: p.name.trim(),
        initial_cash: normalizeAmount(p.initial_cash) ?? p.initial_cash,
        base_currency: p.base_currency.trim().toUpperCase(),
        risk_profile_id: p.risk_profile_id || null,
        broker_link_id: p.broker_link_id || null,
      })),
      ...Object.entries(existing).map(([id, link]) => ({ key: id, portfolio_id: id, broker_link_id: link || null })),
    ];
  }

  function goals(): CampaignGoalSpec[] {
    return (templates ?? [])
      .filter((t) => choices[t.key]?.on)
      .map((t) => ({
        ...t.body,
        target_pct: normalizeAmount(choices[t.key].target) ?? choices[t.key].target,
        closes_positions: choices[t.key].plan,
        portfolio_key: t.portfolio_key,
      }));
  }

  function check(): boolean {
    const e: Record<string, string> = {};
    if (step === 1) {
      if (!header.name.trim()) e.name = "Il nome è obbligatorio";
      if (!header.start_date) e.start_date = "Data di inizio obbligatoria";
      if (!header.end_date) e.end_date = "Data di fine obbligatoria";
      else if (header.end_date < header.start_date) e.end_date = "La data di fine precede quella di inizio";
      else if (header.end_date < today) e.end_date = "La data di fine è già passata";
    }
    if (step === 2) {
      if (!fresh.length && !Object.keys(existing).length) e.portfolios = "Crea o scegli almeno un portafoglio";
      for (const p of fresh) {
        if (!p.name.trim()) e[`${p.key}.name`] = "Il nome è obbligatorio";
        const cash = normalizeAmount(p.initial_cash);
        if (cash === null) e[`${p.key}.initial_cash`] = amountError(p.initial_cash) ?? "Importo non valido";
        else if (!/[1-9]/.test(cash)) e[`${p.key}.initial_cash`] = "Il capitale deve essere maggiore di zero";
        if (!/^[A-Za-z]{3}$/.test(p.base_currency.trim())) e[`${p.key}.base_currency`] = "Codice di 3 lettere (es. EUR)";
      }
    }
    if (step === 3) {
      for (const [key, c] of Object.entries(choices)) {
        if (c.on && normalizeAmount(c.target) === null) e[`goal.${key}`] = "Percentuale non valida";
      }
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  }

  function next() {
    if (!check()) return;
    if (step === 2) {
      loadTemplates.mutate(
        { start_date: header.start_date, end_date: header.end_date, portfolios: specs() },
        {
          onSuccess: (list) => {
            setTemplates(list);
            setChoices((prev) =>
              Object.fromEntries(
                list.map((t) => [
                  t.key,
                  prev[t.key] ?? { on: t.recommended, target: t.body.target_pct, plan: Boolean(t.body.closes_positions) },
                ]),
              ),
            );
            setStep(3);
          },
        },
      );
      return;
    }
    setStep((s) => s + 1);
  }

  function choose(t: CampaignTemplate, change: Partial<GoalChoice>) {
    setChoices((prev) => {
      const out = { ...prev, [t.key]: { ...prev[t.key], ...change } };
      if (change.plan) {
        // at most one piano per portfolio
        for (const other of templates ?? []) {
          if (other.key !== t.key && other.portfolio_key === t.portfolio_key && out[other.key]?.plan) {
            out[other.key] = { ...out[other.key], plan: false };
          }
        }
      }
      return out;
    });
  }

  function confirm(status: "attiva" | "bozza") {
    create.mutate(
      {
        name: header.name.trim(),
        start_date: header.start_date,
        end_date: header.end_date,
        notes: header.notes.trim() || null,
        status,
        portfolios: specs(),
        goals: goals(),
      },
      {
        onSuccess: (c) => {
          toast.success(status === "bozza" ? "Campagna salvata come bozza" : "Campagna creata e avviata");
          onCreated?.(c);
          onClose();
        },
      },
    );
  }

  const setH = (field: keyof typeof header) => (e: { target: { value: string } }) =>
    setHeader((h) => ({ ...h, [field]: e.target.value }));
  const setFreshField = (key: string, field: keyof NewPortfolio, value: string) =>
    setFresh((list) => list.map((p) => (p.key === key ? { ...p, [field]: value } : p)));

  const selectedGoals = goals();

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Nuova campagna</DialogTitle>
          <DialogDescription>
            Passo {step} di {STEPS.length}: {STEPS[step - 1]}
          </DialogDescription>
        </DialogHeader>
        <Stepper step={step} />

        {step === 1 && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <FormField label="Nome della campagna" error={errors.name}>
                <Input value={header.name} onChange={setH("name")} autoFocus placeholder="Prova dicembre" />
              </FormField>
            </div>
            <FormField label="Inizio" error={errors.start_date}>
              <Input type="date" value={header.start_date} onChange={setH("start_date")} />
            </FormField>
            <FormField label="Fine" error={errors.end_date} hint="Fine dei piani della campagna">
              <Input type="date" value={header.end_date} onChange={setH("end_date")} />
            </FormField>
            <div className="space-y-1.5 sm:col-span-2">
              <FormField label="Note (facoltative)">
                <textarea
                  value={header.notes}
                  onChange={setH("notes")}
                  rows={3}
                  className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
                />
              </FormField>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-5">
            <section aria-label="Nuovi portafogli" className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">Nuovi portafogli</h3>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setFresh((list) => [
                      ...list,
                      { key: `new-${counter}`, name: "", initial_cash: "", base_currency: "EUR", risk_profile_id: "", broker_link_id: "" },
                    ]);
                    setCounter((n) => n + 1);
                  }}
                >
                  <Plus />
                  Aggiungi portafoglio
                </Button>
              </div>
              {fresh.map((p, i) => (
                <fieldset key={p.key} aria-label={`Nuovo portafoglio ${i + 1}`} className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
                  <div className="flex items-end gap-2 sm:col-span-2">
                    <div className="flex-1">
                      <FormField label="Nome" error={errors[`${p.key}.name`]}>
                        <Input value={p.name} onChange={(e) => setFreshField(p.key, "name", e.target.value)} />
                      </FormField>
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-label={`Rimuovi il nuovo portafoglio ${i + 1}`}
                      onClick={() => setFresh((list) => list.filter((x) => x.key !== p.key))}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                  <FormField label="Capitale iniziale" error={errors[`${p.key}.initial_cash`]}>
                    <Input
                      value={p.initial_cash}
                      onChange={(e) => setFreshField(p.key, "initial_cash", e.target.value)}
                      inputMode="decimal"
                      placeholder="10000,00"
                    />
                  </FormField>
                  <FormField label="Valuta base" error={errors[`${p.key}.base_currency`]}>
                    <Input value={p.base_currency} onChange={(e) => setFreshField(p.key, "base_currency", e.target.value)} />
                  </FormField>
                  <div className="sm:col-span-2">
                    <FormField label="Profilo di rischio" hint="Vuoto: il profilo Default">
                      <select
                        className={selectClass}
                        value={p.risk_profile_id}
                        onChange={(e) => setFreshField(p.key, "risk_profile_id", e.target.value)}
                      >
                        <option value="">Default</option>
                        {profiles.data?.map((r) => (
                          <option key={r.id} value={r.id}>
                            {r.name}
                          </option>
                        ))}
                      </select>
                    </FormField>
                  </div>
                  <LinkPicker
                    label="Conto di prova (facoltativo)"
                    value={p.broker_link_id}
                    links={links}
                    onChange={(id) => setFreshField(p.key, "broker_link_id", id)}
                  />
                </fieldset>
              ))}
            </section>
            <section aria-label="Portafogli esistenti" className="space-y-2">
              <h3 className="text-sm font-semibold">Portafogli esistenti</h3>
              {candidates.length === 0 && <p className="text-sm text-muted">Nessun portafoglio disponibile.</p>}
              {candidates.map((p) => (
                <div key={p.id} className="space-y-2 rounded-md border p-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={p.id in existing}
                      onChange={(e) =>
                        setExisting((cur) => {
                          const out = { ...cur };
                          if (e.target.checked) out[p.id] = "";
                          else delete out[p.id];
                          return out;
                        })
                      }
                    />
                    {p.name}
                    <span className="text-xs text-muted">{p.mode_label}</span>
                  </label>
                  {p.id in existing && p.broker_link_id && (
                    <p className="pl-6 text-xs text-muted">Già collegato: {p.mode_label}</p>
                  )}
                  {p.id in existing && !p.broker_link_id && (
                    <div className="grid sm:grid-cols-2">
                      <LinkPicker
                        label={`Conto di prova di ${p.name}`}
                        value={existing[p.id]}
                        links={links}
                        onChange={(id) => setExisting((cur) => ({ ...cur, [p.id]: id }))}
                      />
                    </div>
                  )}
                </div>
              ))}
            </section>
            <p className="text-xs text-muted">
              I portafogli collegati a denaro reale non possono far parte di una campagna. Per un nuovo conto paper/demo
              crea prima il collegamento nella pagina{" "}
              <Link to="/broker" className="text-accent hover:underline">
                Broker
              </Link>
              .
            </p>
            {errors.portfolios && (
              <p role="alert" className="text-sm text-loss">
                {errors.portfolios}
              </p>
            )}
          </div>
        )}

        {step === 3 && templates && (
          <div className="space-y-4">
            <p className="text-xs text-muted">
              Gli obiettivi «campagna» vanno dal {formatDay(header.start_date)} al {formatDay(header.end_date)}. Un
              «piano» chiude le posizioni aperte a fine periodo: al massimo un piano per portafoglio.
            </p>
            {[...new Set(templates.map((t) => t.portfolio_key))].map((key) => (
              <section key={key} aria-label={`Obiettivi di ${names[key] ?? key}`} className="space-y-2">
                <h3 className="text-sm font-semibold">{names[key] ?? key}</h3>
                <ul className="space-y-2">
                  {templates
                    .filter((t) => t.portfolio_key === key)
                    .map((t) => {
                      const c = choices[t.key];
                      return (
                        <li key={t.key} className="rounded-md border p-2 text-sm">
                          <label className="flex items-center gap-2">
                            <input type="checkbox" checked={c?.on ?? false} onChange={(e) => choose(t, { on: e.target.checked })} />
                            {t.label}
                          </label>
                          {c?.on && (
                            <div className="mt-2 flex flex-wrap items-end gap-3 pl-6">
                              <FormField
                                label={t.body.kind === "beat_index" ? "Margine (punti)" : "Percentuale"}
                                error={errors[`goal.${t.key}`]}
                              >
                                <Input
                                  value={c.target}
                                  onChange={(e) => choose(t, { target: e.target.value })}
                                  inputMode="decimal"
                                  className="w-24"
                                />
                              </FormField>
                              <label className="flex items-center gap-2 pb-2">
                                <input type="checkbox" checked={c.plan} onChange={(e) => choose(t, { plan: e.target.checked })} />
                                Piano: chiude le posizioni a fine periodo
                              </label>
                            </div>
                          )}
                        </li>
                      );
                    })}
                </ul>
              </section>
            ))}
          </div>
        )}

        {step === 4 && (
          <div className="space-y-3 text-sm">
            <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1">
              <dt className="text-muted">Nome</dt>
              <dd>{header.name.trim()}</dd>
              <dt className="text-muted">Periodo</dt>
              <dd>
                {formatDay(header.start_date)} – {formatDay(header.end_date)}
              </dd>
              {header.notes.trim() && (
                <>
                  <dt className="text-muted">Note</dt>
                  <dd className="whitespace-pre-wrap">{header.notes.trim()}</dd>
                </>
              )}
            </dl>
            <div>
              <h3 className="font-semibold">Portafogli</h3>
              <ul aria-label="Portafogli della campagna" className="list-disc pl-5">
                {specs().map((s) => (
                  <li key={s.key}>
                    {s.portfolio_id ? names[s.key] : `${s.name} (nuovo, ${s.initial_cash} ${s.base_currency})`}
                    {s.broker_link_id
                      ? ` · conto di prova ${links.find((l) => l.id === s.broker_link_id)?.name ?? ""}`
                      : s.portfolio_id
                        ? ""
                        : " · simulato"}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="font-semibold">Obiettivi</h3>
              {selectedGoals.length === 0 ? (
                <p className="text-muted">Nessun obiettivo.</p>
              ) : (
                <ul aria-label="Obiettivi della campagna" className="list-disc pl-5">
                  {(templates ?? [])
                    .filter((t) => choices[t.key]?.on)
                    .map((t) => (
                      <li key={t.key}>
                        {t.body.name} · {names[t.portfolio_key]} · {targetLabel(t, choices[t.key].target)}
                        {choices[t.key].plan ? " · piano" : ""}
                      </li>
                    ))}
                </ul>
              )}
            </div>
            <p className="text-xs text-muted">
              Tutto viene creato insieme: se un passaggio non riesce non resta nulla a metà.
            </p>
          </div>
        )}

        {(loadTemplates.error || create.error) && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(create.error ?? loadTemplates.error)}
          </p>
        )}

        <div className="flex flex-wrap justify-between gap-2">
          <Button type="button" variant="outline" onClick={step === 1 ? onClose : () => setStep((s) => s - 1)} disabled={create.isPending}>
            {step === 1 ? "Annulla" : "Indietro"}
          </Button>
          {step < 4 ? (
            <Button type="button" onClick={next} disabled={loadTemplates.isPending}>
              {loadTemplates.isPending ? "Carico i modelli…" : "Avanti"}
            </Button>
          ) : (
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" onClick={() => confirm("bozza")} disabled={create.isPending}>
                Salva come bozza
              </Button>
              <Button type="button" onClick={() => confirm("attiva")} disabled={create.isPending}>
                {create.isPending ? "Creazione…" : "Crea e avvia"}
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
