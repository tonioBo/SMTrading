import { describe, expect, it } from "vitest";
import { addDays, campaignReportUrl, formatDay, periodReportUrl, romeToday } from "@/lib/campaigns";

describe("campaigns helpers", () => {
  it("formats days and adds calendar days", () => {
    expect(formatDay("2026-11-19")).toBe("19/11/2026");
    expect(formatDay(null)).toBe("—");
    expect(addDays("2026-12-30", 3)).toBe("2027-01-02");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("uses the Rome calendar day", () => {
    expect(romeToday(new Date("2026-11-19T23:30:00Z"))).toBe("2026-11-20");
  });

  it("builds the report URLs", () => {
    expect(periodReportUrl("2026-11-01", "2026-11-19")).toBe("/api/reports/period.pdf?from=2026-11-01&to=2026-11-19");
    expect(periodReportUrl("2026-11-01", "2026-11-19", ["a", "b"])).toBe(
      "/api/reports/period.pdf?from=2026-11-01&to=2026-11-19&portfolio_ids=a%2Cb",
    );
    expect(campaignReportUrl("c1")).toBe("/api/campaigns/c1/report.pdf");
  });
});
