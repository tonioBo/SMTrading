import type { PortfolioStatus } from "@/api/types";
import { Badge } from "@/components/ui/badge";

const PORTFOLIO_STATUS_LABELS: Record<PortfolioStatus, string> = {
  active: "Attivo",
  reduce_only: "Solo riduzione",
  locked: "Bloccato",
  archived: "Archiviato",
};

const VARIANT = { active: "default", reduce_only: "warning", locked: "destructive", archived: "secondary" } as const;

export function PortfolioStatusBadge({ status }: { status: PortfolioStatus }) {
  return <Badge variant={VARIANT[status] ?? "secondary"}>{PORTFOLIO_STATUS_LABELS[status] ?? status}</Badge>;
}
