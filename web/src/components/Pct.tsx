import { formatPct, signTone } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Percentage points, it-IT, signed ("+1,25%"); `tone` colours it green at or above
 * zero, red below. null shows an em dash. */
export function Pct({ value, tone = false, places = 2, className }: { value: string | null | undefined; tone?: boolean; places?: number; className?: string }) {
  if (value == null || value === "") return <span className={cn("num text-muted", className)}>—</span>;
  const t = tone ? signTone(value, places) : null;
  return <span className={cn("num whitespace-nowrap", t === "gain" && "text-gain", t === "loss" && "text-loss", className)}>{formatPct(value, places)}</span>;
}
