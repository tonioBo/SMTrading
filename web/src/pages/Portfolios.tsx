import { Archive, ArchiveRestore, Plus } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { useArchivePortfolio, useCreatePortfolio, usePortfolios, useRiskProfiles } from "@/api/hooks/portfolios";
import type { Portfolio } from "@/api/types";
import { AmountPreview } from "@/components/AmountPreview";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { NameWarning } from "@/components/NameWarning";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { PortfolioStatusBadge } from "@/components/PortfolioStatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { amountError, normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDecimal } from "@/lib/format";

const baseColumns: Column<Portfolio>[] = [
  {
    key: "name",
    header: "Nome",
    sortValue: (p) => p.name,
    cell: (p) => (
      <>
        <Link to={`/portafogli/${p.id}`} className="text-accent hover:underline">
          {p.name}
        </Link>
        <PortfolioModeBadge mode={p.mode} liveState={p.live_state} label={p.mode_label} className="ml-2" />
        {p.valuation_error && <span className="ml-2 text-xs text-loss">valutazione non disponibile</span>}
      </>
    ),
  },
  { key: "equity", header: "Equity", align: "right", cell: (p) => <Money value={p.equity_eur} /> },
  {
    key: "pnl",
    header: "P&L del giorno",
    align: "right",
    cell: (p) => <Pnl value={p.day_pnl_eur} pct={p.day_pnl_pct} />,
  },
  { key: "status", header: "Stato", sortValue: (p) => p.status, cell: (p) => <PortfolioStatusBadge status={p.status} /> },
  {
    key: "leverage",
    header: "Leva",
    align: "right",
    cell: (p) => <span className="num">{formatDecimal(p.leverage, 2)}×</span>,
  },
];

const EMPTY_FORM = {
  name: "",
  initial_cash: "",
  base_currency: "EUR",
  leverage: "1",
  slippage_bps: "0",
  risk_profile_id: "",
  timezone: "Europe/Rome",
};

/** Mounted per opening: archive (or restore) `portfolio` after confirmation. */
function ArchiveDialog({ portfolio, onClose }: { portfolio: Portfolio; onClose: () => void }) {
  const mutation = useArchivePortfolio();
  const archive = portfolio.status !== "archived";
  return (
    <ConfirmDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title={archive ? `Archiviare ${portfolio.name}?` : `Ripristinare ${portfolio.name}?`}
      description={
        archive
          ? "Il portafoglio sparisce da elenchi, dashboard e agenti e resta in sola lettura: niente ordini né job. Registro e posizioni restano invariati. Servono zero ordini aperti e nessun broker collegato."
          : "Il portafoglio torna visibile con lo stato che aveva prima dell'archiviazione."
      }
      confirmLabel={archive ? "Archivia" : "Ripristina"}
      destructive={archive}
      pending={mutation.isPending}
      error={mutation.error ? errorMessage(mutation.error) : null}
      onConfirm={() =>
        mutation.mutate(
          { id: portfolio.id, archive },
          {
            onSuccess: () => {
              toast.success(archive ? "Portafoglio archiviato" : "Portafoglio ripristinato");
              onClose();
            },
          },
        )
      }
    />
  );
}

/** Mounted per opening: the form starts empty each time. */
function CreatePortfolioDialog({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  // saved with a name warning: the dialog stays open on it until the admin moves on
  const [saved, setSaved] = useState<Portfolio | null>(null);
  const profiles = useRiskProfiles();
  const create = useCreatePortfolio();
  const navigate = useNavigate();

  const set = (field: keyof typeof EMPTY_FORM) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const serverField = errorField(create.error);
  const fieldError = (field: string) =>
    localErrors[field] ?? (create.error && serverField === field ? errorMessage(create.error) : null);
  const generalError = create.error && !serverField ? errorMessage(create.error) : null;
  // a server error on a field not in the form is shown as a general error
  const formFields = ["name", "initial_cash", "base_currency", "leverage", "slippage_bps", "risk_profile_id", "timezone"];
  const orphanError = create.error && serverField && !formFields.includes(serverField) ? errorMessage(create.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const errors: Record<string, string> = {};
    if (!form.name.trim()) errors.name = "Il nome è obbligatorio";
    const cash = normalizeAmount(form.initial_cash);
    if (cash === null) errors.initial_cash = amountError(form.initial_cash) ?? "Importo non valido";
    const leverage = normalizeAmount(form.leverage);
    if (leverage === null) errors.leverage = "Leva non valida";
    if (!/^\d+$/.test(form.slippage_bps.trim())) errors.slippage_bps = "Inserisci un intero (bps)";
    setLocalErrors(errors);
    if (Object.keys(errors).length) return;
    create.mutate(
      {
        name: form.name.trim(),
        initial_cash: cash!,
        base_currency: form.base_currency.trim().toUpperCase(),
        leverage: leverage!,
        slippage_bps: Number(form.slippage_bps.trim()),
        timezone: form.timezone.trim(),
        risk_profile_id: form.risk_profile_id || null,
      },
      {
        onSuccess: (p) => {
          if (p.name_warning) {
            setSaved(p);
            return;
          }
          onClose();
          navigate(`/portafogli/${p.id}`);
        },
      },
    );
  }

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Nuovo portafoglio</DialogTitle>
          <DialogDescription>Il saldo iniziale viene registrato come deposito.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
          <div className="space-y-1.5 sm:col-span-2">
            <FormField label="Nome" error={fieldError("name")}>
              <Input value={form.name} onChange={set("name")} autoFocus disabled={saved !== null} />
            </FormField>
            <NameWarning warning={saved?.name_warning} />
          </div>
          <FormField
            label="Saldo iniziale"
            error={fieldError("initial_cash")}
            hint={<AmountPreview input={form.initial_cash} currency={form.base_currency} />}
          >
            <Input value={form.initial_cash} onChange={set("initial_cash")} inputMode="decimal" placeholder="10000,00" />
          </FormField>
          <FormField label="Valuta base" error={fieldError("base_currency")}>
            <Input value={form.base_currency} onChange={set("base_currency")} />
          </FormField>
          <FormField label="Leva" error={fieldError("leverage")} hint="da 1 a 10">
            <Input value={form.leverage} onChange={set("leverage")} inputMode="decimal" />
          </FormField>
          <FormField label="Slippage (bps)" error={fieldError("slippage_bps")}>
            <Input value={form.slippage_bps} onChange={set("slippage_bps")} inputMode="numeric" />
          </FormField>
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
          <FormField label="Fuso orario" error={fieldError("timezone")}>
            <Input value={form.timezone} onChange={set("timezone")} />
          </FormField>
          {(generalError || orphanError) && (
            <p role="alert" className="text-sm text-loss sm:col-span-2">
              {generalError ?? orphanError}
            </p>
          )}
          <DialogFooter className="sm:col-span-2">
            {saved ? (
              <Button
                type="button"
                onClick={() => {
                  onClose();
                  navigate(`/portafogli/${saved.id}`);
                }}
              >
                Apri portafoglio
              </Button>
            ) : (
              <>
                <Button type="button" variant="outline" onClick={onClose} disabled={create.isPending}>
                  Annulla
                </Button>
                <Button type="submit" disabled={create.isPending}>
                  Crea
                </Button>
              </>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function Portfolios() {
  const [showArchived, setShowArchived] = useState(false);
  const { data, isLoading, isError } = usePortfolios({ includeArchived: showArchived });
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState<Portfolio | null>(null);
  const toggleId = useId();

  const columns: Column<Portfolio>[] = [
    ...baseColumns,
    {
      key: "actions",
      header: <span className="sr-only">Azioni</span>,
      align: "right",
      cell: (p) =>
        p.status === "archived" ? (
          <Button size="sm" variant="outline" aria-label={`Ripristina ${p.name}`} onClick={() => setArchiving(p)}>
            <ArchiveRestore />
            Ripristina
          </Button>
        ) : (
          <Button size="sm" variant="ghost" aria-label={`Archivia ${p.name}`} onClick={() => setArchiving(p)}>
            <Archive />
            Archivia
          </Button>
        ),
    },
  ];

  return (
    <div>
      <PageHeader title="Portafogli">
        <div className="flex flex-wrap items-center justify-end gap-4">
          <div className="flex items-center gap-2 text-sm">
            <input
              id={toggleId}
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
              className="h-4 w-4 accent-[rgb(var(--accent))]"
            />
            <label htmlFor={toggleId}>Mostra archiviati</label>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus />
            Nuovo portafoglio
          </Button>
        </div>
      </PageHeader>
      {isError && <p className="mb-4 text-loss">Impossibile caricare i portafogli</p>}
      <Card>
        <CardContent className="pt-6">
          <DataTable
            aria-label="Portafogli"
            columns={columns}
            rows={data}
            rowKey={(p) => p.id}
            loading={isLoading}
            empty="Nessun portafoglio"
          />
        </CardContent>
      </Card>
      {creating && <CreatePortfolioDialog onClose={() => setCreating(false)} />}
      {archiving && <ArchiveDialog portfolio={archiving} onClose={() => setArchiving(null)} />}
    </div>
  );
}
