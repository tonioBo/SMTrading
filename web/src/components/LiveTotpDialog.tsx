import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField } from "@/components/FormField";
import { Input } from "@/components/ui/input";
import { errorField, errorMessage } from "@/lib/errors";
import { TOTP_RE, TOTP_REQUIRED_MESSAGE, isTotpMissing } from "@/lib/live";

/** «Attiva prima l'autenticazione a due fattori» with the way to Impostazioni. */
export function TotpMissingNotice() {
  return (
    <p role="alert" className="text-sm text-loss">
      {TOTP_REQUIRED_MESSAGE}:{" "}
      <Link to="/impostazioni" className="font-medium underline">
        vai a Impostazioni
      </Link>
    </p>
  );
}

/** The 6-digit TOTP field; a server error about the code is shown under it. */
export function TotpField({ value, onChange, error }: { value: string; onChange: (v: string) => void; error: unknown }) {
  const fieldError = error && !isTotpMissing(error) && errorField(error) === "totp" ? errorMessage(error) : null;
  return (
    <FormField label="Codice TOTP" error={fieldError} hint="Le 6 cifre dell'app di autenticazione">
      <Input
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/\D/g, ""))}
      />
    </FormField>
  );
}

/** Errors not shown by TotpField: the missing-TOTP notice or the server message. */
export function TotpErrors({ error }: { error: unknown }) {
  if (!error) return null;
  if (isTotpMissing(error)) return <TotpMissingNotice />;
  if (errorField(error) === "totp") return null;
  return (
    <p role="alert" className="text-sm text-loss">
      {errorMessage(error)}
    </p>
  );
}

interface Props {
  title: string;
  description?: string;
  confirmLabel: string;
  destructive?: boolean;
  pending: boolean;
  /** Keeps the confirm disabled even with a valid code (e.g. nothing chosen yet). */
  confirmDisabled?: boolean;
  error: unknown;
  onClose: () => void;
  onConfirm: (totp: string) => void;
  children?: ReactNode;
}

/** A real-money action that needs the TOTP (link, resume, flatten): mount one per opening. */
export function LiveTotpDialog({
  title,
  description,
  confirmLabel,
  destructive,
  pending,
  confirmDisabled = false,
  error,
  onClose,
  onConfirm,
  children,
}: Props) {
  const [totp, setTotp] = useState("");
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={title}
      description={description}
      confirmLabel={confirmLabel}
      destructive={destructive}
      pending={pending}
      confirmDisabled={confirmDisabled || !TOTP_RE.test(totp)}
      onConfirm={() => onConfirm(totp)}
    >
      {children}
      <TotpField value={totp} onChange={setTotp} error={error} />
      <TotpErrors error={error} />
    </ConfirmDialog>
  );
}
