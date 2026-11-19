import { HeartPulse } from "lucide-react";
import { Link } from "react-router-dom";
import { useSystemHealth } from "@/api/hooks/system";
import { HEALTH_LABEL, HEALTH_TONE } from "@/lib/health";
import { cn } from "@/lib/utils";

const TONE_CLASS = {
  ok: "border-gain/50 text-gain",
  warn: "border-warn/50 text-warn",
  danger: "border-loss/50 text-loss",
  neutral: "border-border text-muted",
  info: "border-accent/50 text-accent",
} as const;

/** Compact «Salute del sistema» status (Dashboard header): a link to the page, the first
 * reason as the tooltip. Nothing while loading or when the view cannot be read. */
export function HealthBadge() {
  const { data } = useSystemHealth();
  if (!data) return null;
  const tone = HEALTH_TONE[data.status] ?? "neutral";
  const extra = data.reasons.length > 1 ? ` (e altri ${data.reasons.length - 1})` : "";
  const title = data.reasons.length ? `${data.reasons[0]}${extra}` : "Salute del sistema: tutto ok";
  return (
    <Link
      to="/salute-sistema"
      title={title}
      aria-label={`Salute del sistema: ${HEALTH_LABEL[data.status] ?? data.status}`}
      className={cn("inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-semibold hover:bg-card", TONE_CLASS[tone])}
    >
      <HeartPulse className="h-3.5 w-3.5" aria-hidden />
      <span>{HEALTH_LABEL[data.status] ?? data.status}</span>
    </Link>
  );
}
