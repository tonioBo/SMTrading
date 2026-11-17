import { LayoutTemplate, Pencil, Plus, Power, RotateCcw } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useDeactivateGoal, useCreateGoal, useGoals, useGoalsProgress, useGoalTemplates, usePatchGoal } from "@/api/hooks/goals";
import { useIsAdmin } from "@/api/hooks/auth";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { Goal, GoalBenchmark, GoalCreate, GoalHorizon, GoalKind, GoalProgress, GoalScope, GoalTemplate } from "@/api/types";
import { AdminOnly } from "@/components/AdminOnly";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, selectClass } from "@/components/FormField";
import { GoalBar } from "@/components/GoalBar";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pct } from "@/components/Pct";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { normalizeAmount, toInputDecimal } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime, formatMoney } from "@/lib/format";
import {
  BENCHMARK_OPTIONS,
  comparisonText,
  gapText,
  goalScopeLabel,
  goalTargetLabel,
  HORIZON_OPTIONS,
  horizonLabel,
  KIND_OPTIONS,
  lossText,
  periodLabel,
  STATUS_TONE,
} from "@/lib/goals";

/** Today in Europe/Rome as "YYYY-MM-DD" (the default start of a new goal). */
function today(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(new Date());
}

interface FormState {
  name: string;
  horizon: GoalHorizon;
  start_date: string;
  end_date: string;
  recurring: boolean;
  scope: GoalScope;
  portfolio_id: string;
  target_pct: string;
  kind: GoalKind;
  benchmark: GoalBenchmark;
}

const DEFAULT_BENCHMARK: GoalBenchmark = "VWCE.DE";

/** The form of an existing goal, or of a new one (empty, or prefilled by a template). */
function initialForm(goal: Goal | GoalCreate | null): FormState {
  if (!goal) {
    return {
      name: "", horizon: "week", start_date: today(), end_date: "", recurring: true, scope: "total",
      portfolio_id: "", target_pct: "2", kind: "min", benchmark: DEFAULT_BENCHMARK,
    };
  }
  return {
    name: goal.name, horizon: goal.horizon, start_date: goal.start_date ?? today(), end_date: goal.end_date ?? "",
    recurring: goal.recurring ?? goal.horizon !== "custom", scope: goal.scope ?? "total", portfolio_id: goal.portfolio_id ?? "",
    target_pct: toInputDecimal(goal.target_pct), kind: goal.kind ?? "min", benchmark: goal.benchmark ?? DEFAULT_BENCHMARK,
  };
}

/** The label of the percentage field: its meaning changes with the kind. */
const TARGET_LABEL: Record<GoalKind, { label: string; hint: string }> = {
  min: { label: "Obiettivo (%)", hint: "es. 2 oppure 2,5" },
  cap: { label: "Obiettivo (%)", hint: "es. 2 oppure 2,5" },
  max_loss: { label: "Limite di perdita (%)", hint: "la perdita massima nel periodo, es. 5" },
  beat_index: { label: "Margine (punti %)", hint: "di quanto fare meglio dell'indice; 0 = basta fare meglio" },
};

const FORM_FIELDS = ["name", "horizon", "start_date", "end_date", "recurring", "scope", "portfolio_id", "target_pct", "kind", "benchmark"];

/** Mounted per opening: create (`goal` null, or `template` to prefill) or edit a goal. */
function GoalDialog({ goal, template, onClose }: { goal: Goal | null; template?: GoalCreate; onClose: () => void }) {
  const [form, setForm] = useState<FormState>(() => initialForm(goal ?? template ?? null));
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const portfolios = usePortfolios();
  const create = useCreateGoal();
  const patch = usePatchGoal();
  const mutation = goal ? patch : create;
  const custom = form.horizon === "custom";

  const set = <K extends keyof FormState>(field: K, value: FormState[K]) => setForm((f) => ({ ...f, [field]: value }));
  /** The index margin starts at 0; leaving it, an untouched 0 goes back to the usual 2. */
  const setKind = (kind: GoalKind) =>
    setForm((f) => {
      let target = f.target_pct;
      if (kind === "beat_index" && f.kind !== "beat_index") target = "0";
      else if (kind !== "beat_index" && f.kind === "beat_index" && (target === "0" || target === "")) target = "2";
      return { ...f, kind, target_pct: target };
    });
  const index = form.kind === "beat_index";
  const targetField = TARGET_LABEL[form.kind];
  const serverField = errorField(mutation.error);
  const fieldError = (field: string) =>
    localErrors[field] ?? (mutation.error && serverField === field ? errorMessage(mutation.error) : null);
  const generalError =
    mutation.error && (!serverField || !FORM_FIELDS.includes(serverField)) ? errorMessage(mutation.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errors: Record<string, string> = {};
    if (!form.name.trim()) errors.name = "Il nome è obbligatorio";
    const pct = normalizeAmount(form.target_pct);
    if (pct === null || (index ? Number(pct) < 0 : Number(pct) <= 0)) errors.target_pct = "Percentuale non valida";
    if (!form.start_date) errors.start_date = "Scegli la data di inizio";
    if (custom && !form.end_date) errors.end_date = "Scegli la data di fine";
    else if (custom && form.end_date < form.start_date) errors.end_date = "La fine viene prima dell'inizio";
    if (form.scope === "portfolio" && !form.portfolio_id) errors.portfolio_id = "Scegli un portafoglio";
    setLocalErrors(errors);
    if (Object.keys(errors).length) return;
    const body: GoalCreate = {
      name: form.name.trim(),
      horizon: form.horizon,
      start_date: form.start_date,
      end_date: custom ? form.end_date : null,
      recurring: custom ? false : form.recurring,
      scope: form.scope,
      portfolio_id: form.scope === "portfolio" ? form.portfolio_id : null,
      target_pct: pct!,
      kind: form.kind,
      benchmark: index ? form.benchmark : null,
    };
    const done = {
      onSuccess: () => {
        toast.success(goal ? "Obiettivo salvato" : "Obiettivo creato");
        onClose();
      },
    };
    if (goal) patch.mutate({ id: goal.id, body }, done);
    else create.mutate(body, done);
  }

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{goal ? `Modifica ${goal.name}` : "Nuovo obiettivo"}</DialogTitle>
          <DialogDescription>
            Andamento del valore nel periodo, misurato dall&apos;apertura del primo giorno. I periodi seguono il calendario (ora italiana).
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="sm:col-span-2">
            <FormField label="Nome" error={fieldError("name")} hint="un nome breve, es. «mensile» o «Natale»">
              <Input value={form.name} onChange={(e) => set("name", e.target.value)} maxLength={80} autoFocus />
            </FormField>
          </div>
          <FormField label="Orizzonte" error={fieldError("horizon")}>
            <select className={selectClass} value={form.horizon} onChange={(e) => set("horizon", e.target.value as GoalHorizon)}>
              {HORIZON_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </FormField>
          {custom ? (
            <div />
          ) : (
            <div className="flex items-end gap-2 pb-2 text-sm">
              <input
                id="goal-recurring"
                type="checkbox"
                checked={form.recurring}
                onChange={(e) => set("recurring", e.target.checked)}
                className="h-4 w-4 accent-[rgb(var(--accent))]"
              />
              <label htmlFor="goal-recurring">Ripeti ogni periodo</label>
            </div>
          )}
          <FormField
            label="Dal"
            error={fieldError("start_date")}
            hint={custom ? undefined : "conta il periodo di calendario che contiene questa data"}
          >
            <Input type="date" value={form.start_date} onChange={(e) => set("start_date", e.target.value)} />
          </FormField>
          {custom ? (
            <FormField label="Al" error={fieldError("end_date")} hint="incluso">
              <Input type="date" value={form.end_date} onChange={(e) => set("end_date", e.target.value)} />
            </FormField>
          ) : (
            <div />
          )}
          <FormField label="Ambito" error={fieldError("scope")}>
            <select className={selectClass} value={form.scope} onChange={(e) => set("scope", e.target.value as GoalScope)}>
              <option value="total">Totale (tutti i portafogli)</option>
              <option value="portfolio">Un portafoglio</option>
            </select>
          </FormField>
          {form.scope === "portfolio" ? (
            <FormField label="Portafoglio" error={fieldError("portfolio_id")}>
              <select className={selectClass} value={form.portfolio_id} onChange={(e) => set("portfolio_id", e.target.value)}>
                <option value="">Scegli…</option>
                {portfolios.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </FormField>
          ) : (
            <div />
          )}
          <FormField label="Tipo" error={fieldError("kind")} hint={KIND_OPTIONS.find((o) => o.value === form.kind)?.hint}>
            <select className={selectClass} value={form.kind} onChange={(e) => setKind(e.target.value as GoalKind)}>
              {KIND_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label={targetField.label} error={fieldError("target_pct")} hint={targetField.hint}>
            <Input value={form.target_pct} onChange={(e) => set("target_pct", e.target.value)} inputMode="decimal" />
          </FormField>
          {index && (
            <FormField label="Indice" error={fieldError("benchmark")} hint="confrontato in euro sullo stesso periodo">
              <select className={selectClass} value={form.benchmark} onChange={(e) => set("benchmark", e.target.value as GoalBenchmark)}>
                {BENCHMARK_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </FormField>
          )}
          {generalError && (
            <p role="alert" className="text-sm text-loss sm:col-span-2">
              {generalError}
            </p>
          )}
          <DialogFooter className="sm:col-span-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={mutation.isPending}>
              Annulla
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {goal ? "Salva" : "Crea"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeactivateDialog({ goal, onClose }: { goal: Goal; onClose: () => void }) {
  const mutation = useDeactivateGoal();
  return (
    <ConfirmDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={`Disattivare l'obiettivo ${goal.name}?`}
      description="Sparisce da dashboard, grafico, riepiloghi Telegram e agenti. Resta nell'elenco dei disattivati e si può riattivare."
      confirmLabel="Disattiva"
      destructive
      pending={mutation.isPending}
      error={mutation.error ? errorMessage(mutation.error) : null}
      onConfirm={() =>
        mutation.mutate(goal.id, {
          onSuccess: () => {
            toast.success("Obiettivo disattivato");
            onClose();
          },
        })
      }
    />
  );
}

/** The bar and the figures under it, by kind: a minimum/cap shows the target in euros and
 * the gap; a risk goal the loss against its limit; an index goal the index's % and the
 * difference in points (no bar: there is no fixed target). */
function GoalFigures({ goal, p }: { goal: Goal; p: GoalProgress }) {
  const elapsed = <span>{`tempo trascorso ${Math.round(Number(p.elapsed_pct))}%`}</span>;
  if (p.kind === "beat_index") {
    return (
      <p className="flex flex-wrap justify-between gap-2 text-xs text-muted">
        <span>{comparisonText(p)}</span>
        {elapsed}
      </p>
    );
  }
  if (p.kind === "max_loss") {
    const loss = lossText(p);
    return (
      <>
        <GoalBar pct={p.loss_pct ?? null} target={p.target_pct} kind="max_loss" elapsed={p.elapsed_pct} label={`Perdita ${goal.name}`} />
        <p className="flex flex-wrap justify-between gap-2 text-xs text-muted">
          <span>
            {[p.target_eur == null ? null : `limite di perdita ${formatMoney(p.target_eur, "EUR")}`, loss].filter(Boolean).join(" · ")}
          </span>
          {elapsed}
        </p>
      </>
    );
  }
  const gap = gapText(p);
  return (
    <>
      <GoalBar pct={p.pct} target={p.target_pct} kind={p.kind} elapsed={p.elapsed_pct} label={`Avanzamento ${goal.name}`} />
      <p className="flex flex-wrap justify-between gap-2 text-xs text-muted">
        <span>
          {p.kind === "cap" ? "tetto " : "obiettivo "}
          <Money value={p.target_eur} />
          {gap && ` · ${gap}`}
        </span>
        {elapsed}
      </p>
    </>
  );
}

/** «Modelli»: the server's ready goals; choosing one opens the dialog prefilled. */
function TemplatesDialog({ onChoose, onClose }: { onChoose: (t: GoalTemplate) => void; onClose: () => void }) {
  const { data, isLoading, isError } = useGoalTemplates(true);
  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Modelli</DialogTitle>
          <DialogDescription>Scegli un modello: apre il modulo già compilato, da controllare e confermare.</DialogDescription>
        </DialogHeader>
        {isError ? (
          <p className="text-sm text-loss">Impossibile caricare i modelli</p>
        ) : isLoading ? (
          <p className="text-sm text-muted">Caricamento…</p>
        ) : !data?.length ? (
          <p className="text-sm text-muted">Nessun modello disponibile</p>
        ) : (
          <ul className="grid gap-2" aria-label="Modelli di obiettivo">
            {data.map((t) => (
              <li key={t.key}>
                <Button variant="outline" className="w-full justify-between" aria-label={t.label} onClick={() => onChoose(t)}>
                  <span>{t.label}</span>
                  <span className="num text-xs text-muted">{goalTargetLabel(t.body.kind ?? "min", t.body.target_pct, t.body.benchmark)}</span>
                </Button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}

function GoalCard({ goal, progress, onEdit, onDeactivate }: { goal: Goal; progress: GoalProgress | undefined; onEdit: () => void; onDeactivate: () => void }) {
  const admin = useIsAdmin();
  const p = progress;
  return (
    <Card role="article" aria-label={`Obiettivo ${goal.name}`}>
      <CardHeader className="space-y-1 pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            {goal.name}
            <span className="num text-base font-semibold text-muted">{goalTargetLabel(goal.kind, goal.target_pct, goal.benchmark)}</span>
          </CardTitle>
          {p ? (
            <StatusBadge tone={STATUS_TONE[p.status] ?? "neutral"}>{p.status}</StatusBadge>
          ) : (
            goal.portfolio_archived && <StatusBadge tone="neutral">portafoglio archiviato</StatusBadge>
          )}
        </div>
        <p className="text-xs text-muted">
          {[goalScopeLabel(goal), horizonLabel(goal), p && goal.horizon !== "custom" ? periodLabel(p.period_start, p.period_end) : null]
            .filter(Boolean)
            .join(" · ")}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {!p ? (
          <p className="text-sm text-muted">Avanzamento non disponibile</p>
        ) : p.start_eur == null ? (
          <p className="text-sm text-muted">Dati del periodo non ancora disponibili</p>
        ) : (
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="num text-lg font-semibold">{`${formatMoney(p.start_eur, "EUR")} → ${p.current_eur == null ? "—" : formatMoney(p.current_eur, "EUR")}`}</span>
              <Pct value={p.pct} tone />
            </div>
            <GoalFigures goal={goal} p={p} />
          </div>
        )}
        {admin && (
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="ghost" aria-label={`Modifica ${goal.name}`} onClick={onEdit}>
              <Pencil />
              Modifica
            </Button>
            <Button size="sm" variant="ghost" aria-label={`Disattiva ${goal.name}`} onClick={onDeactivate}>
              <Power />
              Disattiva
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function InactiveGoals({ goals, onEdit }: { goals: Goal[]; onEdit: (g: Goal) => void }) {
  const admin = useIsAdmin();
  const patch = usePatchGoal();
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle>Disattivati</CardTitle>
      </CardHeader>
      <CardContent>
        <Table aria-label="Obiettivi disattivati">
          <TableHeader>
            <TableRow>
              <TableHead>Nome</TableHead>
              <TableHead>Obiettivo</TableHead>
              <TableHead>Ambito</TableHead>
              <TableHead>Periodo</TableHead>
              <TableHead>Modificato</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {goals.map((g) => (
              <TableRow key={g.id}>
                <TableCell className="font-medium">{g.name}</TableCell>
                <TableCell className="num">{goalTargetLabel(g.kind, g.target_pct, g.benchmark)}</TableCell>
                <TableCell>{goalScopeLabel(g)}</TableCell>
                <TableCell>{horizonLabel(g)}</TableCell>
                <TableCell className="whitespace-nowrap text-xs text-muted">{formatDateTime(g.updated_at)}</TableCell>
                <TableCell className="text-right">
                  {admin && (
                    <div className="flex justify-end gap-2">
                      <Button size="sm" variant="ghost" aria-label={`Modifica ${g.name}`} onClick={() => onEdit(g)}>
                        <Pencil />
                        Modifica
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        aria-label={`Riattiva ${g.name}`}
                        disabled={patch.isPending || g.portfolio_archived}
                        title={g.portfolio_archived ? "il portafoglio è archiviato" : undefined}
                        onClick={() =>
                          patch.mutate(
                            { id: g.id, body: { active: true } },
                            {
                              onSuccess: () => toast.success("Obiettivo riattivato"),
                              onError: (err) => toast.error(errorMessage(err)),
                            },
                          )
                        }
                      >
                        <RotateCcw />
                        Riattiva
                      </Button>
                    </div>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export default function Goals() {
  const { data, isLoading, isError } = useGoals();
  const progress = useGoalsProgress();
  const [editing, setEditing] = useState<Goal | null | undefined>(undefined); // null = new
  const [deactivating, setDeactivating] = useState<Goal | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [template, setTemplate] = useState<GoalCreate | undefined>(undefined);
  const byId = new Map((progress.data ?? []).map((p) => [p.goal_id, p]));
  const active = data?.filter((g) => g.active) ?? [];
  const inactive = data?.filter((g) => !g.active) ?? [];

  return (
    <div>
      <PageHeader title="Obiettivi">
        <AdminOnly>
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="outline" onClick={() => setChoosing(true)}>
              <LayoutTemplate />
              Modelli
            </Button>
            <Button
              onClick={() => {
                setTemplate(undefined);
                setEditing(null);
              }}
            >
              <Plus />
              Nuovo obiettivo
            </Button>
          </div>
        </AdminOnly>
      </PageHeader>
      <p className="mb-4 text-sm text-muted">
        Crescita del valore da raggiungere (minimo) o da non superare (tetto), perdita massima nel periodo, oppure fare meglio di un indice; sul totale o su un portafoglio. Li leggono anche gli agenti e i riepiloghi Telegram; il rischio viene sempre prima.
      </p>
      {isError && <p className="mb-4 text-loss">Impossibile caricare gli obiettivi</p>}
      {progress.isError && <p className="mb-4 text-loss">Impossibile caricare l&apos;avanzamento</p>}
      {isLoading ? (
        <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        data && (
          <div className="space-y-6">
            {active.length === 0 ? (
              <p className="text-sm text-muted">Nessun obiettivo attivo</p>
            ) : (
              <div className="grid gap-4 lg:grid-cols-2">
                {active.map((g) => (
                  <GoalCard key={g.id} goal={g} progress={byId.get(g.id)} onEdit={() => setEditing(g)} onDeactivate={() => setDeactivating(g)} />
                ))}
              </div>
            )}
            {inactive.length > 0 && <InactiveGoals goals={inactive} onEdit={setEditing} />}
          </div>
        )
      )}
      {choosing && (
        <TemplatesDialog
          onClose={() => setChoosing(false)}
          onChoose={(t) => {
            setChoosing(false);
            setTemplate(t.body);
            setEditing(null);
          }}
        />
      )}
      {editing !== undefined && <GoalDialog goal={editing} template={editing ? undefined : template} onClose={() => setEditing(undefined)} />}
      {deactivating && <DeactivateDialog goal={deactivating} onClose={() => setDeactivating(null)} />}
    </div>
  );
}
