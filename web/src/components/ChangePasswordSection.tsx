import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useChangePassword } from "@/api/hooks/auth";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

const MIN_LEN = 10;

/** `forced`: the first login with a temporary password (nothing else is reachable until
 * it is changed). */
export function ChangePasswordSection({ forced = false }: { forced?: boolean }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const change = useChangePassword();

  const newError =
    next.length > 0 && next.length < MIN_LEN
      ? "Almeno 10 caratteri"
      : next.length > 0 && next === current
        ? "La nuova password deve essere diversa da quella attuale"
        : null;
  const confirmError = confirm.length > 0 && confirm !== next ? "Le password non coincidono" : null;
  const wrongCurrent = change.error instanceof ApiError && change.error.code === "FORBIDDEN";
  const currentError = wrongCurrent ? "Password attuale errata" : submitted && !current ? "Campo obbligatorio" : null;
  const generalError = change.error && !wrongCurrent ? errorMessage(change.error) : null;

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (!current || !next || newError || confirm !== next) return;
    change.mutate(
      { current, new: next },
      {
        onSuccess: () => {
          setCurrent("");
          setNext("");
          setConfirm("");
          setSubmitted(false);
          toast.success(forced ? "Password aggiornata: benvenuto in SMTrading" : "Password aggiornata. Le altre sessioni sono state chiuse.");
        },
      },
    );
  }

  return (
    <Card className={forced ? undefined : "mt-6"}>
      <CardHeader>
        <CardTitle>Cambia password</CardTitle>
      </CardHeader>
      <CardContent>
        {forced && (
          <p className="mb-3 text-sm text-muted">
            Stai usando una password temporanea: scegline una nuova (almeno 10 caratteri) per continuare.
          </p>
        )}
        <form onSubmit={onSubmit} className="max-w-sm space-y-3" noValidate>
          <FormField label={forced ? "Password temporanea" : "Password attuale"} error={currentError}>
            <Input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
          </FormField>
          <FormField label="Nuova password" error={newError}>
            <Input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
          </FormField>
          <FormField label="Conferma nuova password" error={confirmError}>
            <Input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </FormField>
          {generalError && (
            <p role="alert" className="text-sm text-loss">
              {generalError}
            </p>
          )}
          <Button type="submit" disabled={change.isPending}>
            Aggiorna password
          </Button>
          {!forced && <p className="text-xs text-muted">Ricorda di aggiornare SMT_ADMIN_PASSWORD nel file .env per i test end-to-end.</p>}
        </form>
      </CardContent>
    </Card>
  );
}
