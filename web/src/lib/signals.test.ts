import { describe, expect, it } from "vitest";
import type { SignalSymbol } from "@/api/types";
import { goalProgress, signalBadge } from "@/lib/signals";

function row(over: Partial<SignalSymbol>): SignalSymbol {
  return {
    symbol: "AAPL", last: "100", indicator: "RSI(14)", value_closed: "40", value_now: "38",
    threshold: "30", direction: "below", distance: "10", triggered_closed: false, triggered_now: false,
    as_of: "2026-11-16T15:00:00Z", ...over,
  };
}

describe("signalBadge", () => {
  it("prefers «scatterebbe ora», then the closed-bar signal", () => {
    expect(signalBadge(row({ triggered_now: true, triggered_closed: true })).label).toBe("scatterebbe ora");
    expect(signalBadge(row({ triggered_closed: true })).label).toBe("segnale sulla chiusura");
  });

  it("near within 20% of the threshold (below / above)", () => {
    expect(signalBadge(row({ distance: "6" })).label).toBe("vicino");
    expect(signalBadge(row({ distance: "-6" })).label).toBe("vicino");
    expect(signalBadge(row({ distance: "6.01" })).label).toBe("lontano");
    expect(signalBadge(row({ direction: "above", threshold: "-50", distance: "10" })).label).toBe("vicino");
  });

  it("cross_up: near within 2% of the price", () => {
    const cross = { direction: "cross_up" as const, threshold: "100", last: "200" };
    expect(signalBadge(row({ ...cross, distance: "-4" })).label).toBe("vicino");
    expect(signalBadge(row({ ...cross, distance: "-4.5" })).label).toBe("lontano");
  });

  it("far when the distance is unknown", () => {
    expect(signalBadge(row({ distance: "" })).label).toBe("lontano");
    expect(signalBadge(row({ distance: "n/d" })).label).toBe("lontano");
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
