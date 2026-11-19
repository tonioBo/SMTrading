import type { TradeOutcome } from "@/api/types";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { OUTCOME_LABELS } from "@/lib/trades";

const TONE: Record<TradeOutcome, StatusTone> = { win: "ok", loss: "danger", open: "info" };

/** «Vincente», «Perdente», «Aperta»: the outcome of a trade of the «Diario operazioni». */
export function OutcomeBadge({ outcome }: { outcome: TradeOutcome }) {
  return <StatusBadge tone={TONE[outcome]}>{OUTCOME_LABELS[outcome]}</StatusBadge>;
}
