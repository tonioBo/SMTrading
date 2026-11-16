import type { PortfolioStatus } from "@/api/types";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";

const PORTFOLIO_STATUS_LABELS: Record<PortfolioStatus, string> = {
  active: "Attivo",
  reduce_only: "Solo riduzione",
  locked: "Bloccato",
  archived: "Archiviato",
};

const TONE: Record<PortfolioStatus, StatusTone> = { active: "ok", reduce_only: "warn", locked: "danger", archived: "neutral" };

export function PortfolioStatusBadge({ status }: { status: PortfolioStatus }) {
  return <StatusBadge tone={TONE[status] ?? "neutral"}>{PORTFOLIO_STATUS_LABELS[status] ?? status}</StatusBadge>;
}
