import { describe, expect, it } from "vitest";
import { formatDay, gapText, goalScopeLabel, goalTargetLabel, horizonLabel, periodLabel, STATUS_TONE } from "@/lib/goals";

describe("goals labels", () => {
  it("target: signed %, trailing zeros dropped, «tetto» for a cap", () => {
    expect(goalTargetLabel("min", "2")).toBe("+2%");
    expect(goalTargetLabel("min", "2.5")).toBe("+2,5%");
    expect(goalTargetLabel("min", "10")).toBe("+10%");
    expect(goalTargetLabel("cap", "3")).toBe("tetto +3%");
  });

  it("horizon: recurring reads «ogni …», custom shows its dates", () => {
    expect(horizonLabel({ horizon: "week", recurring: true, start_date: "2026-01-05", end_date: null })).toBe("ogni settimana");
    expect(horizonLabel({ horizon: "quarter", recurring: false, start_date: "2026-10-01", end_date: null })).toBe("trimestre");
    expect(horizonLabel({ horizon: "custom", recurring: false, start_date: "2026-11-01", end_date: "2026-12-24" })).toBe(
      "dal 01/11/2026 al 24/12/2026",
    );
  });

  it("scope, days and periods", () => {
    expect(goalScopeLabel({ scope: "total", portfolio_name: null })).toBe("Totale");
    expect(goalScopeLabel({ scope: "portfolio", portfolio_name: "Alfa" })).toBe("Alfa");
    expect(formatDay("2026-11-16")).toBe("16/11/2026");
    expect(periodLabel("2026-11-16", "2026-11-22")).toBe("16/11/2026 – 22/11/2026");
  });

  it("gap: what is missing or over, by kind", () => {
    expect(gapText({ kind: "min", gap_eur: "10" })).toBe("mancano 10,00 €");
    expect(gapText({ kind: "min", gap_eur: "-4.5" })).toBe("superato di 4,50 €");
    expect(gapText({ kind: "cap", gap_eur: "3" })).toBe("margine 3,00 €");
    expect(gapText({ kind: "cap", gap_eur: "-6" })).toBe("oltre di 6,00 €");
    expect(gapText({ kind: "min", gap_eur: null })).toBeNull();
  });

  it("status tones", () => {
    expect(STATUS_TONE.raggiunto).toBe("ok");
    expect(STATUS_TONE["in ritardo"]).toBe("warn");
    expect(STATUS_TONE["oltre il tetto"]).toBe("danger");
    expect(STATUS_TONE["non iniziato"]).toBe("neutral");
  });
});
