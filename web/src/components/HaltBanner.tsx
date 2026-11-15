import { OctagonPause } from "lucide-react";
import { useState } from "react";
import { coveringHalts, useClearHalt, useHalts } from "@/api/hooks/halts";
import type { TradingHalt } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { useIsAdmin } from "@/api/hooks/auth";

/** "Aperture sospese dal CEO — <motivo> — dal <data>" (portfolio scope: its name first). */
function haltText(h: TradingHalt): string {
  const where = h.scope === "global" ? "" : ` su ${h.portfolio ?? "un portafoglio"}`;
  return `Aperture sospese dal CEO${where} — ${h.reason} — dal ${formatDateTime(h.set_at)}`;
}

/** «Revoca» with a confirmation: clears the halt (admin). */
export function RevokeHaltButton({ halt }: { halt: TradingHalt }) {
  const [open, setOpen] = useState(false);
  const clear = useClearHalt();
  const admin = useIsAdmin();
  if (!admin) return null;
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        Revoca
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o);
          if (!o) clear.reset();
        }}
        title="Revocare il blocco aperture?"
        description={`Le nuove aperture tornano possibili${halt.scope === "global" ? " su tutti i portafogli" : ` su ${halt.portfolio ?? "questo portafoglio"}`}.`}
        confirmLabel="Revoca"
        pending={clear.isPending}
        error={clear.error ? errorMessage(clear.error) : null}
        onConfirm={() => clear.mutate(halt.id, { onSuccess: () => setOpen(false) })}
      />
    </>
  );
}

/** The active halts covering a portfolio (all of them without `portfolioId`). */
export function HaltBanner({ portfolioId }: { portfolioId?: string }) {
  const { data } = useHalts();
  const halts = coveringHalts(data?.active, portfolioId);
  if (!halts.length) return null;
  return (
    <div className="mb-4 space-y-2">
      {halts.map((h) => (
        <div key={h.id} role="alert" aria-label="Blocco aperture" className="flex flex-wrap items-center gap-3 rounded-md border border-loss/40 bg-loss/15 px-4 py-2 text-sm text-loss">
          <OctagonPause className="h-4 w-4 shrink-0" aria-hidden />
          <span className="flex-1 font-medium">
            {haltText(h)}
            {h.set_by_name && <span className="ml-2 text-xs font-normal opacity-80">(impostato da {h.set_by_name})</span>}
          </span>
          <RevokeHaltButton halt={h} />
        </div>
      ))}
    </div>
  );
}
