import type { CampaignStatus } from "@/api/types";

export const CAMPAIGN_STATUS_LABELS: Record<CampaignStatus, string> = {
  bozza: "Bozza",
  attiva: "Attiva",
  conclusa: "Conclusa",
};

/** Today in Europe/Rome as "yyyy-mm-dd". */
export function romeToday(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Rome" }).format(now);
}

/** "yyyy-mm-dd" `days` later (calendar days, no time zone involved). */
export function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d + days));
  return date.toISOString().slice(0, 10);
}

/** "yyyy-mm-dd" → "dd/mm/yyyy" (anything else unchanged). */
export function formatDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** `GET /api/reports/period.pdf`: the «Resoconto» of a period (every live portfolio when
 * `portfolioIds` is empty). */
export function periodReportUrl(from: string, to: string, portfolioIds: string[] = []): string {
  const qs = new URLSearchParams({ from, to });
  if (portfolioIds.length) qs.set("portfolio_ids", portfolioIds.join(","));
  return `/api/reports/period.pdf?${qs.toString()}`;
}

/** `GET /api/campaigns/{id}/report.pdf`: the campaign's «Resoconto». */
export function campaignReportUrl(id: string): string {
  return `/api/campaigns/${id}/report.pdf`;
}
