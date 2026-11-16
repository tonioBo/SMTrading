import { describe, expect, it } from "vitest";
import type { SignalExit, SignalsResponse, SignalSymbol } from "@/api/types";
import { closestSignals, exitBadge, goalProgress, signalBadge } from "@/lib/signals";

function row(over: Partial<SignalSymbol>): SignalSymbol {
  return {
    symbol: "AAPL", last: "100", indicator: "RSI(14)", value_closed: "40", value_now: "38",
    threshold: "30", direction: "below", distance: "10", triggered_closed: false, triggered_now: false,
    as_of: "2026-11-16T15:00:00Z", ...over,
  };
}

describe("signalBadge", () => {
  it("an open position, then a resting entry, take precedence over the signal", () => {
    const fired = { triggered_now: true, triggered_closed: true };
    expect(signalBadge(row({ ...fired, position: { qty: "0.51", avg_price: "258" }, open_entry: true }))).toMatchObject({
      tone: "held", icon: "📌", label: "in posizione",
    });
    expect(signalBadge(row({ ...fired, position: null, open_entry: true }))).toMatchObject({
      tone: "pending", icon: "⏳", label: "ordine in attesa",
    });
    expect(signalBadge(row({ ...fired, position: null, open_entry: false })).label).toBe("scatterebbe ora");
  });

  it("prefers «scatterebbe ora», then the closed-bar signal", () => {
    expect(signalBadge(row({ triggered_now: true, triggered_closed: true })).label).toBe("scatterebbe ora");
    expect(signalBadge(row({ triggered_closed: true })).label).toBe("segnale sulla chiusura");
  });

  it("near within 20% of the threshold (below / above), never at or past it", () => {
    expect(signalBadge(row({ distance: "6" })).label).toBe("vicino");
    expect(signalBadge(row({ distance: "0.01" })).label).toBe("vicino");
    expect(signalBadge(row({ distance: "-6" })).label).toBe("lontano");
    expect(signalBadge(row({ distance: "0" })).label).toBe("lontano");
    expect(signalBadge(row({ distance: "6.01" })).label).toBe("lontano");
    expect(signalBadge(row({ direction: "above", threshold: "-50", distance: "10" })).label).toBe("vicino");
    expect(signalBadge(row({ direction: "above", threshold: "50", distance: "-1" })).label).toBe("lontano");
  });

  it("cross_up: near within 2% of the price while the fast average is still below", () => {
    const cross = { direction: "cross_up" as const, threshold: "100", last: "200" };
    expect(signalBadge(row({ ...cross, distance: "4" })).label).toBe("vicino");
    expect(signalBadge(row({ ...cross, distance: "4.5" })).label).toBe("lontano");
  });

  it("cross_up already above without a fresh cross: neutral, waits for a new cross", () => {
    const cross = { direction: "cross_up" as const, threshold: "100", last: "200" };
    for (const distance of ["-4", "0", "-50"]) {
      expect(signalBadge(row({ ...cross, distance }))).toMatchObject({
        tone: "above", label: "già sopra · attende un nuovo incrocio",
      });
    }
    // a fresh cross is still a signal
    expect(signalBadge(row({ ...cross, distance: "-4", triggered_now: true })).label).toBe("scatterebbe ora");
  });

  it("far when the distance is unknown", () => {
    expect(signalBadge(row({ distance: "" })).label).toBe("lontano");
    expect(signalBadge(row({ distance: "n/d" })).label).toBe("lontano");
  });
});

describe("exitBadge", () => {
  const exit = (over: Partial<SignalExit>): SignalExit => ({
    rule: "RSI 2 sopra 70", value: "60", threshold: "70", distance: "10", triggered: false, ...over,
  });

  it("fires, near, or nothing (RSI: 20% of the threshold)", () => {
    expect(exitBadge("rsi_reversion", exit({ triggered: true, distance: "-5" }))).toMatchObject({
      tone: "fires", label: "uscita scatterebbe",
    });
    expect(exitBadge("rsi_reversion", exit({ distance: "14" }))?.label).toBe("uscita vicina");
    expect(exitBadge("rsi_reversion", exit({ distance: "14.5" }))).toBeNull();
    expect(exitBadge("rsi_reversion", exit({ distance: "0" }))).toBeNull();
  });

  it("price rules (sma_cross, breakout): near within 2% of the threshold", () => {
    const price = { rule: "SMA 2 sotto SMA 5", value: "102", threshold: "100" };
    expect(exitBadge("sma_cross", exit({ ...price, distance: "2" }))?.label).toBe("uscita vicina");
    expect(exitBadge("breakout", exit({ ...price, distance: "2.5" }))).toBeNull();
  });

  it("nothing without an exit", () => {
    expect(exitBadge("rsi_reversion", null)).toBeNull();
    expect(exitBadge("rsi_reversion", undefined)).toBeNull();
  });
});

describe("closestSignals", () => {
  const strategy = (ref: string, symbols: SignalSymbol[]) => ({
    ref, name: ref, type: "rsi_reversion", params: {}, timeframe: "1d", status: "ok" as const, symbols,
  });
  const resp: SignalsResponse = {
    as_of: "2026-11-16T15:00:00Z",
    portfolios: [
      {
        portfolio_id: "p1", portfolio_name: "Alfa",
        strategies: [strategy("S1", [
          row({ symbol: "FAR", distance: "20" }),
          row({ symbol: "NEAR2", distance: "3" }), // 3 / (30 * 0.2) = 0.5
          row({ symbol: "HELD", distance: "1", position: { qty: "1", avg_price: "1" } }),
          row({ symbol: "PEND", distance: "1", open_entry: true }),
          row({ symbol: "NODATA", distance: null }),
        ])],
      },
      {
        portfolio_id: "p2", portfolio_name: "Beta",
        strategies: [strategy("S2", [
          row({ symbol: "NOW", distance: "-2", triggered_now: true }),
          row({ symbol: "ABOVE", direction: "cross_up", last: "100", threshold: "90", distance: "-10" }),
          row({ symbol: "CROSS", direction: "cross_up", last: "100", threshold: "101", distance: "0.5" }), // 0.25
          row({ symbol: "NEAR1", distance: "1.2" }), // 0.2
        ])],
      },
    ],
  };

  it("fired first, then by distance relative to the «vicino» band; no position, pending, already-above or unknown", () => {
    const top = closestSignals(resp, 5);
    expect(top.map((t) => t.symbol.symbol)).toEqual(["NOW", "NEAR1", "CROSS", "NEAR2", "FAR"]);
    expect(top[0]).toMatchObject({ portfolioId: "p2", portfolioName: "Beta", ref: "S2" });
    expect(closestSignals(resp, 2)).toHaveLength(2);
    expect(closestSignals(undefined, 5)).toEqual([]);
  });
});

describe("goalProgress", () => {
  it("is the share of the target, clamped to 0..100", () => {
    expect(goalProgress("1", "2")).toBe(50);
    expect(goalProgress("3", "2")).toBe(100);
    expect(goalProgress("-1", "2")).toBe(0);
    expect(goalProgress(null, "2")).toBe(0);
  });
});
