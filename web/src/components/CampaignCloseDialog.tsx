import { FileDown } from "lucide-react";
import { useState } from "react";
import { useCampaignCloseCheck, useCloseCampaign } from "@/api/hooks/campaigns";
import type { Campaign } from "@/api/types";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { campaignReportUrl, formatDay } from "@/lib/campaigns";
import { errorMessage } from "@/lib/errors";
import { formatQty } from "@/lib/format";

const PLAN_STATE: Record<string, string> = {
  pending: "in attesa",
  closing: "in chiusura",
  closed: "chiusa",
};

/** «Chiudi campagna»: what is still open (positions, orders, pianos), which portfolios
 * to archive (the archive rules apply: a blocked one cannot be ticked), then the final
 * report. */
export function CampaignCloseDialog({ campaign, onClose }: { campaign: Campaign; onClose: () => void }) {
  const check = useCampaignCloseCheck(campaign.id);
  const close = useCloseCampaign();
  const [archive, setArchive] = useState<string[]>([]);
  const [notes, setNotes] = useState(campaign.notes ?? "");
  const done = close.data?.status === "conclusa";
  const data = check.data;

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Chiudi la campagna «{campaign.name}»</DialogTitle>
          <DialogDescription>
            {formatDay(campaign.start_date)} – {formatDay(campaign.end_date)}
          </DialogDescription>
        </DialogHeader>

        {done ? (
          <div className="space-y-3 text-sm">
            <p role="status" className="text-gain">
              Campagna conclusa.
            </p>
            <Button asChild>
              <a href={campaignReportUrl(campaign.id)} download>
                <FileDown />
                Scarica il resoconto finale
              </a>
            </Button>
            <div>
              <Button type="button" variant="outline" onClick={onClose}>
                Chiudi
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4 text-sm">
            {check.isLoading && <p className="text-muted">Verifica in corso…</p>}
            {check.error && (
              <p role="alert" className="text-loss">
                {errorMessage(check.error)}
              </p>
            )}
            {data && (
              <>
                <p>
                  Posizioni aperte: <strong>{data.open_positions}</strong> · ordini aperti:{" "}
                  <strong>{data.open_orders}</strong>
                </p>
                {data.plans.length > 0 && (
                  <section aria-label="Chiusura dei piani" className="space-y-1">
                    <h3 className="font-semibold">Chiusura dei piani</h3>
                    {data.plans.map((p) => (
                      <div key={`${p.goal_id}-${p.period_start}`}>
                        <p>
                          {p.goal_name ?? "piano"} ({formatDay(p.period_start)} – {formatDay(p.period_end)}):{" "}
                          {p.counts.closed} chiuse, {p.counts.pending + p.counts.closing} ancora da chiudere
                        </p>
                        <ul className="pl-5 text-xs text-muted">
                          {p.positions.map((x) => (
                            <li key={`${x.portfolio_id}-${x.symbol}`}>
                              {x.portfolio_name} · {x.symbol}: {PLAN_STATE[x.state] ?? x.state}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </section>
                )}
                <section aria-label="Portafogli da archiviare" className="space-y-2">
                  <h3 className="font-semibold">Portafogli</h3>
                  {data.portfolios.map((p) => (
                    <div key={p.id} className="space-y-1 rounded-md border p-2">
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          disabled={!p.can_archive}
                          checked={archive.includes(p.id)}
                          onChange={(e) =>
                            setArchive((cur) => (e.target.checked ? [...cur, p.id] : cur.filter((x) => x !== p.id)))
                          }
                        />
                        Archivia {p.name}
                        {p.archived && <span className="text-xs text-muted">(già archiviato)</span>}
                      </label>
                      {p.archive_blocker && <p className="pl-6 text-xs text-warn">Non archiviabile: {p.archive_blocker}</p>}
                      {p.positions.length > 0 && (
                        <p className="pl-6 text-xs text-muted">
                          Posizioni: {p.positions.map((x) => `${x.symbol} ${formatQty(x.qty)}`).join(", ")}
                        </p>
                      )}
                      {p.open_orders.length > 0 && (
                        <p className="pl-6 text-xs text-muted">
                          Ordini aperti: {p.open_orders.map((o) => `${o.symbol} ${o.side} ${o.type}`).join(", ")}
                        </p>
                      )}
                    </div>
                  ))}
                </section>
                <FormField label="Note finali (facoltative)">
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    rows={3}
                    className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
                  />
                </FormField>
              </>
            )}
            {close.error && (
              <p role="alert" className="text-loss">
                {errorMessage(close.error)}
              </p>
            )}
            <div className="flex flex-wrap justify-between gap-2">
              <Button type="button" variant="outline" onClick={onClose} disabled={close.isPending}>
                Annulla
              </Button>
              <Button
                type="button"
                disabled={!data || close.isPending}
                onClick={() => close.mutate({ id: campaign.id, archive, notes })}
              >
                {close.isPending ? "Chiusura…" : "Concludi la campagna"}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
