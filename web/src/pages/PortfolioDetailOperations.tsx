import { ArrowDownToLine, ArrowUpFromLine, RotateCcw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { useCashMove, useIdempotencyKey, useResetPortfolio } from "@/api/hooks/portfolios";
import type { Portfolio } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { normalizeAmount } from "@/lib/decimal";
import { errorField, errorMessage } from "@/lib/errors";
import { formatMoney } from "@/lib/format";

const CASH_MOVE = {
  deposit: { title: "Deposito", confirm: "Deposita", done: "Deposito registrato" },
  withdraw: { title: "Prelievo", confirm: "Preleva", done: "Prelievo registrato" },
} as const;

/** Mounted per opening: one Idempotency-Key per opening, reused by its retries. */
function CashMoveDialog({ portfolio, kind, onClose }: { portfolio: Portfolio; kind: "deposit" | "withdraw"; onClose: () => void }) {
  const text = CASH_MOVE[kind];
  const key = useIdempotencyKey();
  const move = useCashMove(portfolio.id, kind);
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState(portfolio.base_currency);
  const [reason, setReason] = useState("");
  const [invalid, setInvalid] = useState(false);
  const serverField = errorField(move.error);
  const amountError = invalid
    ? "Inserisci un importo positivo"
    : move.error && serverField === "amount"
      ? errorMessage(move.error)
      : null;

  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={text.title}
      description={`Portafoglio ${portfolio.name}`}
      confirmLabel={text.confirm}
      pending={move.isPending}
      error={move.error && serverField !== "amount" ? errorMessage(move.error) : null}
      onConfirm={() => {
        const value = normalizeAmount(amount);
        const ok = value !== null && /[1-9]/.test(value);
        setInvalid(!ok);
        if (!ok) return;
        move.mutate(
          { body: { amount: value, currency: currency.trim().toUpperCase(), reason: reason.trim() || undefined }, key },
          {
            onSuccess: () => {
              toast.success(`${text.done}: ${formatMoney(value, currency.trim().toUpperCase())}`);
              onClose();
            },
          },
        );
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label="Importo" error={amountError}>
          <Input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder="1000,00" autoFocus />
        </FormField>
        <FormField label="Valuta">
          <Input value={currency} onChange={(e) => setCurrency(e.target.value)} />
        </FormField>
        <div className="sm:col-span-2">
          <FormField label="Motivazione (facoltativa)">
            <Input value={reason} onChange={(e) => setReason(e.target.value)} />
          </FormField>
        </div>
      </div>
    </ConfirmDialog>
  );
}

function ResetDialog({ portfolio, onClose }: { portfolio: Portfolio; onClose: () => void }) {
  const key = useIdempotencyKey();
  const reset = useResetPortfolio(portfolio.id);
  const [cash, setCash] = useState("");
  const [currency, setCurrency] = useState(portfolio.base_currency);
  const [reason, setReason] = useState("");
  const [confirmName, setConfirmName] = useState("");
  const [errors, setErrors] = useState<{ cash?: string; reason?: string }>({});

  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Reset del portafoglio"
      description="Annulla gli ordini aperti, chiude tutte le posizioni a mercato, azzera i saldi e deposita il nuovo capitale. Non si può annullare."
      confirmLabel="Esegui reset"
      destructive
      pending={reset.isPending}
      confirmDisabled={confirmName !== portfolio.name}
      error={reset.error ? errorMessage(reset.error) : null}
      onConfirm={() => {
        const value = normalizeAmount(cash);
        const next = {
          cash: value === null ? "Importo non valido" : undefined,
          reason: reason.trim() ? undefined : "La motivazione è obbligatoria",
        };
        setErrors(next);
        if (next.cash || next.reason) return;
        reset.mutate(
          { body: { new_cash: value!, currency: currency.trim().toUpperCase(), reason: reason.trim() }, key },
          {
            onSuccess: (res) => {
              toast.success(
                `Reset eseguito: ${res.canceled_orders} ordini annullati, ${res.closing_orders.length} posizioni chiuse`,
              );
              onClose();
            },
          },
        );
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label="Nuovo capitale" error={errors.cash}>
          <Input value={cash} onChange={(e) => setCash(e.target.value)} inputMode="decimal" placeholder="10000,00" autoFocus />
        </FormField>
        <FormField label="Valuta">
          <Input value={currency} onChange={(e) => setCurrency(e.target.value)} />
        </FormField>
        <div className="sm:col-span-2">
          <FormField label="Motivazione" error={errors.reason}>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} />
          </FormField>
        </div>
        <div className="sm:col-span-2">
          <FormField label={`Scrivi «${portfolio.name}» per confermare`}>
            <Input value={confirmName} onChange={(e) => setConfirmName(e.target.value)} autoComplete="off" />
          </FormField>
        </div>
      </div>
    </ConfirmDialog>
  );
}

type Dialog = "deposit" | "withdraw" | "reset" | null;

export function Operations({ portfolio }: { portfolio: Portfolio }) {
  const [dialog, setDialog] = useState<Dialog>(null);
  const close = () => setDialog(null);

  return (
    <div className="grid gap-4 md:grid-cols-3">
      <Card>
        <CardHeader>
          <CardTitle>Deposito</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted">
          <p>Aggiunge liquidità al portafoglio.</p>
          <Button onClick={() => setDialog("deposit")}>
            <ArrowDownToLine />
            Nuovo deposito
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Prelievo</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted">
          <p>Ritira liquidità dal portafoglio.</p>
          <Button variant="outline" onClick={() => setDialog("withdraw")}>
            <ArrowUpFromLine />
            Nuovo prelievo
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Reset</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted">
          <p>Chiude tutto e riparte da un nuovo capitale.</p>
          <Button variant="destructive" onClick={() => setDialog("reset")}>
            <RotateCcw />
            Reset portafoglio
          </Button>
        </CardContent>
      </Card>
      {(dialog === "deposit" || dialog === "withdraw") && <CashMoveDialog portfolio={portfolio} kind={dialog} onClose={close} />}
      {dialog === "reset" && <ResetDialog portfolio={portfolio} onClose={close} />}
    </div>
  );
}
