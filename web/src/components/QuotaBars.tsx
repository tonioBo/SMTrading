import type { ProviderQuota } from "@/api/types";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const DEMOTED_LABEL: Record<string, string> = {
  "429": "ha risposto 429 (quota esaurita)",
  day: "quota giornaliera quasi esaurita",
  hour: "quota oraria quasi esaurita",
};

function Bar({ label, used, total, pct, name }: { label: string; used: number; total: number; pct: number; name: string }) {
  const width = Math.min(Math.max(pct, 0), 100);
  return (
    <div className="space-y-0.5">
      <div className="flex justify-between gap-2 text-xs">
        <span className="text-muted">{label}</span>
        <span className="num">
          {used} / {total}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${label} ${name}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(width)}
        className="h-1.5 w-full overflow-hidden rounded-full bg-border"
      >
        <div className={cn("h-full rounded-full", pct >= 90 ? "bg-loss" : pct >= 70 ? "bg-warn" : "bg-accent")} style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

/** The calls of the minute / hour / UTC day against the provider's budgets (only the budgets it has). */
export function QuotaBars({ name, quota }: { name: string; quota: ProviderQuota }) {
  const bars: [string, number, number | null, number | null][] = [
    ["Al minuto", quota.used_minute, quota.per_min, quota.pct_minute],
    ["All'ora", quota.used_hour, quota.per_hour, quota.pct_hour],
    ["Al giorno (UTC)", quota.used_day, quota.per_day, quota.pct_day],
  ];
  return (
    <div className="space-y-1.5" aria-label={`Quota ${name}`}>
      {bars.map(([label, used, total, pct]) => (total ? <Bar key={label} label={label} used={used} total={total} pct={pct ?? 0} name={name} /> : null))}
      {quota.demoted && (
        <p className="text-xs text-warn">
          {`Usato per ultimo: ${DEMOTED_LABEL[quota.demoted_reason ?? ""] ?? "quota"}`}
          {quota.demoted_until ? ` fino a ${formatDateTime(quota.demoted_until)}` : ""}
        </p>
      )}
    </div>
  );
}
