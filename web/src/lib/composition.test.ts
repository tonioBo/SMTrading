import { describe, expect, it } from "vitest";
import type { CompositionPosition } from "@/api/types";
import { MAX_SLICES, donutSlices } from "@/lib/composition";

function pos(symbol: string, value: string | null, weight: string | null): CompositionPosition {
  return {
    symbol, name: symbol, asset_class: "us_equity", market: "NASDAQ", currency: "USD", qty: "1",
    avg_price: "1", last: value, value_eur: value, weight_pct: weight, pnl_eur: "0", pnl_pct: "0", stale: false,
  };
}

describe("donutSlices", () => {
  it("keeps the biggest positions, groups the rest in «Altri» and adds «Contante» last", () => {
    const positions = Array.from({ length: 11 }, (_, i) => pos(`S${i}`, `${110 - i * 10}.5`, `${11 - i}.1`));
    const slices = donutSlices(positions, { key: "cash", label: "Contante", value_eur: "100", weight_pct: "10" });
    expect(slices.map((s) => s.label)).toEqual([...positions.slice(0, MAX_SLICES).map((p) => p.symbol), "Altri (3)", "Contante"]);
    const others = slices[MAX_SLICES];
    // S8 + S9 + S10 = 30.5 + 20.5 + 10.5, weights 3.1 + 2.1 + 1.1 (exact decimal sums)
    expect(others.value_eur).toBe("61.5");
    expect(others.weight_pct).toBe("6.3");
  });

  it("leaves out positions without a value and needs no «Altri» when few", () => {
    const slices = donutSlices([pos("A", "10", "50"), pos("B", null, null)], undefined);
    expect(slices.map((s) => s.key)).toEqual(["A"]);
  });
});
