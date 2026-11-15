import { useState } from "react";
import { useIsAdmin } from "@/api/hooks/auth";
import { usePaperclipStatus, useReconfirmPaperclip } from "@/api/hooks/paperclip";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errors";
import { PAPERCLIP_ERRORS_THRESHOLD, expiryText, isolationText } from "@/lib/paperclip";

/** «Riconferma» with a confirmation: the current isolation rules become the expected ones (admin). */
function ReconfirmButton() {
  const [open, setOpen] = useState(false);
  const reconfirm = useReconfirmPaperclip();
  return (
    <>
      <Button size="sm" variant="outline" className="border-white/60 bg-transparent text-white hover:bg-white/10" onClick={() => setOpen(true)}>
        Riconferma
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o);
          if (!o) reconfirm.reset();
        }}
        title="Riconfermare le regole di isolamento?"
        description="Le regole attuali di Paperclip diventano quelle attese. Fallo solo dopo aver controllato le modifiche in Paperclip."
        confirmLabel="Riconferma"
        pending={reconfirm.isPending}
        error={reconfirm.error ? errorMessage(reconfirm.error) : null}
        onConfirm={() => reconfirm.mutate(undefined, { onSuccess: () => setOpen(false) })}
      />
    </>
  );
}

/** Global Paperclip notices under the header (admins and viewers): isolation, reachability, key expiry. */
export function PaperclipBanner() {
  const { data } = usePaperclipStatus();
  const admin = useIsAdmin();
  if (!data || !data.enabled) return null;
  return (
    <>
      {data.isolation === "changed" && (
        <div
          role="alert"
          aria-label="Isolamento Paperclip"
          className="flex flex-wrap items-center justify-center gap-3 border-b border-loss bg-loss px-6 py-2 text-sm font-semibold text-white"
        >
          <span>{isolationText(data.changes)}</span>
          {admin && <ReconfirmButton />}
        </div>
      )}
      {data.consecutive_errors >= PAPERCLIP_ERRORS_THRESHOLD && (
        <div role="alert" aria-label="Paperclip non raggiungibile" className="border-b border-warn/40 bg-warn/15 px-6 py-1.5 text-center text-xs font-semibold text-warn">
          Paperclip non raggiungibile
          {data.last_error && <span className="font-normal"> ({data.last_error})</span>}
        </div>
      )}
      {data.key_expiring && (
        <div role="alert" aria-label="Scadenza chiave Paperclip" className="border-b border-warn/40 bg-warn/15 px-6 py-1.5 text-center text-xs font-semibold text-warn">
          {expiryText(data.key_expires_at)}
        </div>
      )}
    </>
  );
}
