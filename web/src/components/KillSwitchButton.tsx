import { useMutation, useQueryClient } from "@tanstack/react-query";
import { OctagonX, ShieldCheck } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import { apiFetch } from "@/api/client";
import { dashboardKey } from "@/api/hooks/dashboard";
import { killSwitchKey, useKillSwitch } from "@/api/hooks/killSwitch";
import type { KillSwitchResult } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";

/** Red button that turns the kill-switch on (optionally cancelling open orders) or off. */
export function KillSwitchButton() {
  const { data } = useKillSwitch();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const checkId = useId();
  const turningOn = !data?.on;

  const mutation = useMutation({
    mutationFn: () =>
      apiFetch<KillSwitchResult>("/api/kill-switch", {
        method: "POST",
        body: { on: turningOn, cancel_open: turningOn && cancelOpen },
      }),
    onSuccess: (res) => {
      qc.setQueryData(killSwitchKey, { on: res.on });
      void qc.invalidateQueries({ queryKey: dashboardKey });
      toast.success(
        res.on
          ? `Kill-switch attivato${res.canceled ? `: ${res.canceled} ordini annullati` : ""}`
          : "Kill-switch disattivato",
      );
      setOpen(false);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Operazione non riuscita"),
  });

  if (!data) return null;

  return (
    <>
      <Button
        variant={turningOn ? "destructive" : "outline"}
        onClick={() => {
          setCancelOpen(false);
          setOpen(true);
        }}
      >
        {turningOn ? <OctagonX /> : <ShieldCheck />}
        {turningOn ? "Attiva kill-switch" : "Disattiva kill-switch"}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={turningOn ? "Attivare il kill-switch?" : "Disattivare il kill-switch?"}
        description={
          turningOn
            ? "Tutti i nuovi ordini degli agenti verranno bloccati."
            : "Gli agenti potranno tornare a inviare ordini."
        }
        confirmLabel={turningOn ? "Attiva" : "Disattiva"}
        destructive={turningOn}
        pending={mutation.isPending}
        onConfirm={() => mutation.mutate()}
      >
        {turningOn && (
          <div className="flex items-center gap-2 text-sm">
            <input
              id={checkId}
              type="checkbox"
              checked={cancelOpen}
              onChange={(e) => setCancelOpen(e.target.checked)}
              className="h-4 w-4 accent-[rgb(var(--accent))]"
            />
            <label htmlFor={checkId}>Annulla anche gli ordini aperti</label>
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}
