import { describe, expect, it } from "vitest";
import {
  benchmarkLabel,
  BENCHMARK_OPTIONS,
  comparisonText,
  formatDay,
  gapText,
  goalScopeLabel,
  goalTargetLabel,
  horizonLabel,
  KIND_OPTIONS,
  lossText,
  periodLabel,
  pointsText,
  STATUS_TONE,
} from "@/lib/goals";

describe("goals labels", () => {
  it("target: signed %, trailing zeros dropped, «tetto» for a cap", () => {
    expect(goalTargetLabel("min", "2")).toBe("+2%");
    expect(goalTargetLabel("min", "2.5")).toBe("+2,5%");
    expect(goalTargetLabel("min", "10")).toBe("+10%");
    expect(goalTargetLabel("cap", "3")).toBe("tetto +3%");
  });

  it("target of the risk and index goals", () => {
    expect(goalTargetLabel("max_loss", "5")).toBe("perdita max 5%");
    expect(goalTargetLabel("max_loss", "2.5")).toBe("perdita max 2,5%");
    expect(goalTargetLabel("beat_index", "0", "VWCE.DE")).toBe("meglio di VWCE");
    expect(goalTargetLabel("beat_index", "1.5", "SPY")).toBe("meglio di SPY +1,5 pt");
    expect(goalTargetLabel("beat_index", "0", null)).toBe("meglio dell'indice");
  });

  it("kinds and benchmarks offered by the dialog", () => {
    expect(KIND_OPTIONS.map((o) => [o.value, o.label])).toEqual([
      ["min", "minimo"],
      ["cap", "tetto"],
      ["max_loss", "perdita massima"],
      ["beat_index", "meglio dell'indice"],
    ]);
    expect(BENCHMARK_OPTIONS.map((o) => [o.value, o.label])).toEqual([
      ["SPY", "SPY (S&P 500)"],
      ["QQQ", "QQQ (Nasdaq 100)"],
      ["VWCE.DE", "VWCE (ETF mondiale)"],
    ]);
    expect(benchmarkLabel("VWCE.DE")).toBe("VWCE");
    expect(benchmarkLabel("QQQ")).toBe("QQQ");
  });

  it("points, loss and comparison texts", () => {
    expect(pointsText("1.25")).toBe("+1,25 pt");
    expect(pointsText("-0.5")).toBe("-0,50 pt");
    expect(lossText({ kind: "max_loss", loss_pct: "1.2", target_pct: "5" })).toBe("perdita 1,20% su un limite di 5%");
    expect(lossText({ kind: "max_loss", loss_pct: null, target_pct: "5" })).toBeNull();
    expect(lossText({ kind: "min", loss_pct: "1", target_pct: "5" })).toBeNull();
    expect(comparisonText({ kind: "beat_index", benchmark: "VWCE.DE", benchmark_pct: "2", diff_pp: "1" })).toBe(
      "VWCE +2,00% · differenza +1,00 pt",
    );
    expect(comparisonText({ kind: "beat_index", benchmark: "SPY", benchmark_pct: null, diff_pp: null })).toBe(
      "SPY: dati non ancora disponibili",
    );
    expect(comparisonText({ kind: "min", benchmark: null, benchmark_pct: null, diff_pp: null })).toBeNull();
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
    expect(gapText({ kind: "max_loss", gap_eur: "10" })).toBeNull();
    expect(gapText({ kind: "beat_index", gap_eur: "10" })).toBeNull();
  });

  it("status tones", () => {
    expect(STATUS_TONE.raggiunto).toBe("ok");
    expect(STATUS_TONE["in ritardo"]).toBe("warn");
    expect(STATUS_TONE["oltre il tetto"]).toBe("danger");
    expect(STATUS_TONE["non iniziato"]).toBe("neutral");
    expect(STATUS_TONE["nel limite"]).toBe("ok");
    expect(STATUS_TONE["vicino al limite"]).toBe("warn");
    expect(STATUS_TONE.superato).toBe("danger");
    expect(STATUS_TONE.avanti).toBe("ok");
    expect(STATUS_TONE["in linea"]).toBe("info");
    expect(STATUS_TONE.indietro).toBe("warn");
  });
});
