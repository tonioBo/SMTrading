import { useState } from "react";
import { toast } from "sonner";
import { useArmLiveLink } from "@/api/hooks/live";
import type { LiveLink } from "@/api/types";
import { FormField } from "@/components/FormField";
import { TotpErrors, TotpField } from "@/components/LiveTotpDialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { errorField, errorMessage } from "@/lib/errors";
import { TOTP_RE, liveKindInfo } from "@/lib/live";

const CHECKS = [
  { key: "accept_risks", label: "Ho letto e accetto i rischi" },
  { key: "dedicated_account", label: "Il conto è dedicato a SMTrading" },
  { key: "limits_set", label: "Ho impostato i limiti del profilo di rischio" },
] as const;
type CheckKey = (typeof CHECKS)[number]["key"];

interface Props {
  link: LiveLink;
  /** the linked portfolio's cash, formatted (placeholder {liquidità}) */
  cash: string;
  /** the linked portfolio's risk profile (placeholder {profilo}) */
  profile: string;
  onClose: () => void;
}

/**
 * «Attiva denaro reale»: the warning of the spec (§9) with its placeholders filled, three
 * confirmations, the link's name typed and the TOTP. Mount one per opening.
 */
export function LiveArmDialog({ link, cash, profile, onClose }: Props) {
  const arm = useArmLiveLink(link.id);
  const [checks, setChecks] = useState<Record<CheckKey, boolean>>({ accept_risks: false, dedicated_account: false, limits_set: false });
  const [name, setName] = useState("");
  const [totp, setTotp] = useState("");
  const broker = liveKindInfo(link.kind)?.broker ?? link.kind;
  const account = link.account_masked ?? "conto non ancora letto";
  const ready = CHECKS.every((c) => checks[c.key]) && name.trim() === link.name && TOTP_RE.test(totp);
  const field = errorField(arm.error);
  const nameError = field === "confirm_name" ? errorMessage(arm.error) : null;

  function submit() {
    arm.mutate(
      { totp, confirm_name: name.trim(), ...checks },
      {
        onSuccess: () => {
          toast.success(`Denaro reale attivato su ${link.name}`);
          onClose();
        },
        onError: () => setTotp(""), // a code is accepted once: type the next one
      },
    );
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !arm.isPending && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto border-loss sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Attiva denaro reale · {link.name}</DialogTitle>
          <DialogDescription className="sr-only">Conferma l'attivazione del trading con denaro reale</DialogDescription>
        </DialogHeader>
        <div role="note" aria-label="Avviso di attivazione" className="space-y-2 rounded-md border border-loss/40 bg-loss/10 p-3 text-sm">
          <p className="font-semibold">Stai per attivare il trading con denaro reale.</p>
          <p>
            Da questo momento gli agenti potranno inviare ordini <strong>veri</strong> al tuo conto {broker} ({account}),{" "}
            <strong>senza chiederti conferma per ogni ordine</strong>, entro i limiti del portafoglio collegato: liquidità {cash} e
            profilo di rischio «{profile}».
          </p>
          <p>
            Puoi perdere in tutto o in parte il denaro investito. I risultati in simulazione o in paper non garantiscono risultati
            reali. Prezzi, commissioni e cambi reali possono differire dalle stime.
          </p>
          <p>
            Riceverai una notifica Telegram per ogni ordine. Puoi fermare tutto in qualsiasi momento con «Disattiva denaro reale» o
            con il kill switch.
          </p>
        </div>
        <fieldset className="space-y-2">
          <legend className="sr-only">Conferme obbligatorie</legend>
          {CHECKS.map((c) => (
            <div key={c.key} className="flex items-center gap-2 text-sm">
              <input
                id={`arm-${link.id}-${c.key}`}
                type="checkbox"
                checked={checks[c.key]}
                onChange={(e) => setChecks((x) => ({ ...x, [c.key]: e.target.checked }))}
                className="h-4 w-4 accent-[rgb(var(--loss))]"
              />
              <label htmlFor={`arm-${link.id}-${c.key}`}>{c.label}</label>
            </div>
          ))}
        </fieldset>
        <p className="text-sm">Scrivi il nome del collegamento e inserisci il codice TOTP per confermare.</p>
        <FormField label="Nome del collegamento" error={nameError} hint={`Scrivi «${link.name}»`}>
          <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </FormField>
        <TotpField value={totp} onChange={setTotp} error={arm.error} />
        {field !== "confirm_name" && <TotpErrors error={arm.error} />}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={arm.isPending}>
            Annulla
          </Button>
          <Button variant="destructive" onClick={submit} disabled={!ready || arm.isPending}>
            Attiva
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
