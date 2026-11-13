import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useCreatePortfolio, usePortfolios, useRiskProfiles } from "@/api/hooks/portfolios";
import type { Portfolio } from "@/api/types";
import { AmountPreview } from "@/components/AmountPreview";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { PortfolioStatusBadge } from "@/components/PortfolioStatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { amountError, normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDecimal } from "@/lib/format";

const columns: Column<Portfolio>[] = [
  {
    key: "name",
    header: "Nome",
    sortValue: (p) => p.name,
    cell: (p) => (
      <>
        <Link to={`/portafogli/${p.id}`} className="text-accent hover:underline">
          {p.name}
        </Link>
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

/** Mounted per opening: the form starts empty each time. */
function CreatePortfolioDialog({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
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
          <div className="sm:col-span-2">
            <FormField label="Nome" error={fieldError("name")}>
              <Input value={form.name} onChange={set("name")} autoFocus />
            </FormField>
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
            <Button type="button" variant="outline" onClick={onClose} disabled={create.isPending}>
              Annulla
            </Button>
            <Button type="submit" disabled={create.isPending}>
              Crea
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function Portfolios() {
  const { data, isLoading, isError } = usePortfolios();
  const [creating, setCreating] = useState(false);

  return (
    <div>
      <PageHeader title="Portafogli">
        <Button onClick={() => setCreating(true)}>
          <Plus />
          Nuovo portafoglio
        </Button>
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
    </div>
  );
}
