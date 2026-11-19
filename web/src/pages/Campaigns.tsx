import { FileDown, Play, Plus, Square } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { useCampaigns, usePatchCampaign } from "@/api/hooks/campaigns";
import type { Campaign } from "@/api/types";
import { AdminOnly } from "@/components/AdminOnly";
import { CampaignCloseDialog } from "@/components/CampaignCloseDialog";
import { CampaignWizard } from "@/components/CampaignWizard";
import { PageHeader } from "@/components/PageHeader";
import { ReportButton } from "@/components/ReportDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CAMPAIGN_STATUS_LABELS, campaignReportUrl, formatDay } from "@/lib/campaigns";
import { errorMessage } from "@/lib/errors";
import { formatDecimal } from "@/lib/format";

const STATUS_VARIANT = { attiva: "default", bozza: "warning", conclusa: "secondary" } as const;

function goalText(g: Campaign["goals"][number]): string {
  const pct = formatDecimal(g.target_pct, 2);
  const target = g.kind === "max_loss" ? `−${pct}%` : g.kind === "beat_index" ? `+${pct} pp su ${g.benchmark ?? ""}` : `${pct}%`;
  return `${g.name} · ${g.portfolio_name ?? "totale"} · ${target}${g.closes_positions ? " · piano" : ""}${g.active ? "" : " · disattivato"}`;
}

function CampaignCard({ campaign, onClose }: { campaign: Campaign; onClose: () => void }) {
  const patch = usePatchCampaign();
  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2 space-y-0">
        <div className="space-y-1">
          <CardTitle className="text-lg">{campaign.name}</CardTitle>
          <p className="text-sm text-muted">
            {formatDay(campaign.start_date)} – {formatDay(campaign.end_date)}
            {campaign.closed_at && ` · conclusa da ${campaign.closed_by ?? "—"}`}
          </p>
        </div>
        <Badge variant={STATUS_VARIANT[campaign.status]}>{CAMPAIGN_STATUS_LABELS[campaign.status]}</Badge>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {campaign.notes && <p className="whitespace-pre-wrap text-muted">{campaign.notes}</p>}
        <div>
          <h3 className="font-medium">Portafogli</h3>
          <ul className="list-disc pl-5">
            {campaign.portfolios.map((p) => (
              <li key={p.id}>
                <Link to={`/portafogli/${p.id}`} className="text-accent hover:underline">
                  {p.name}
                </Link>
                {p.created && <span className="text-muted"> · creato dalla campagna</span>}
                {p.broker_link_name && <span className="text-muted"> · conto di prova {p.broker_link_name}</span>}
                {p.status === "archived" && <span className="text-muted"> · archiviato</span>}
              </li>
            ))}
          </ul>
        </div>
        {campaign.goals.length > 0 && (
          <div>
            <h3 className="font-medium">Obiettivi</h3>
            <ul className="list-disc pl-5">
              {campaign.goals.map((g) => (
                <li key={g.id}>{goalText(g)}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <Button asChild variant="outline" size="sm">
            <a href={campaignReportUrl(campaign.id)} download>
              <FileDown />
              Scarica resoconto
            </a>
          </Button>
          <AdminOnly>
            {campaign.status === "bozza" && (
              <Button
                size="sm"
                variant="outline"
                disabled={patch.isPending}
                onClick={() =>
                  patch.mutate(
                    { id: campaign.id, body: { status: "attiva" } },
                    {
                      onSuccess: () => toast.success("Campagna avviata"),
                      onError: (e) => toast.error(errorMessage(e)),
                    },
                  )
                }
              >
                <Play />
                Avvia
              </Button>
            )}
            {campaign.status !== "conclusa" && (
              <Button size="sm" variant="outline" onClick={onClose}>
                <Square />
                Chiudi campagna
              </Button>
            )}
          </AdminOnly>
        </div>
      </CardContent>
    </Card>
  );
}

export default function Campaigns() {
  const { data, isLoading, isError } = useCampaigns();
  const [creating, setCreating] = useState(false);
  const [closing, setClosing] = useState<Campaign | null>(null);
  return (
    <div>
      <PageHeader title="Campagne">
        <div className="flex flex-wrap justify-end gap-2">
          <ReportButton />
          <AdminOnly>
            <Button onClick={() => setCreating(true)}>
              <Plus />
              Nuova campagna
            </Button>
          </AdminOnly>
        </div>
      </PageHeader>
      <p className="mb-4 text-sm text-muted">
        Una campagna è una prova con un nome e un periodo: raggruppa i portafogli, i loro obiettivi e, a fine prova, il
        resoconto PDF.
      </p>
      {isError && <p className="mb-4 text-loss">Impossibile caricare le campagne</p>}
      {isLoading && <p className="text-muted">Caricamento…</p>}
      {data && data.length === 0 && <p className="text-muted">Nessuna campagna</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        {data?.map((c) => (
          <CampaignCard key={c.id} campaign={c} onClose={() => setClosing(c)} />
        ))}
      </div>
      {creating && <CampaignWizard onClose={() => setCreating(false)} />}
      {closing && <CampaignCloseDialog campaign={closing} onClose={() => setClosing(null)} />}
    </div>
  );
}
