import { afterEach, describe, expect, it } from "vitest";
import { tileStyle } from "@/components/markets/labels";
import { DEFAULT_PREFS, MARKETS_PREFS_KEY, highlightParts, readPrefs, writePrefs } from "@/lib/marketsPrefs";

afterEach(() => localStorage.clear());

describe("marketsPrefs", () => {
  it("round-trips and checks every stored value", () => {
    expect(readPrefs()).toEqual(DEFAULT_PREFS);
    writePrefs({ ...DEFAULT_PREFS, sector: "Energia", sort: "change", dir: "desc", quick: "recent", mapOpen: false });
    expect(readPrefs()).toMatchObject({ sector: "Energia", sort: "change", dir: "desc", quick: "recent", mapOpen: false });
    localStorage.setItem(MARKETS_PREFS_KEY, JSON.stringify({ sort: "price", dir: "up", quick: "hot", asset_class: "bonds", sector: 3, mapOpen: "no" }));
    expect(readPrefs()).toEqual(DEFAULT_PREFS);
    localStorage.setItem(MARKETS_PREFS_KEY, "{not json");
    expect(readPrefs()).toEqual(DEFAULT_PREFS);
  });

  it("highlights every case-insensitive occurrence", () => {
    expect(highlightParts("Banca Intesa", "an")).toEqual([
      { text: "B", hit: false },
      { text: "an", hit: true },
      { text: "ca Intesa", hit: false },
    ]);
    expect(highlightParts("AAPL", "")).toEqual([{ text: "AAPL", hit: false }]);
    expect(highlightParts("aaa", "A")).toEqual([
      { text: "a", hit: true },
      { text: "a", hit: true },
      { text: "a", hit: true },
    ]);
  });

  it("colours a tile by the sign and size of the change", () => {
    expect(tileStyle("3").backgroundColor).toBe("rgb(var(--gain) / 0.80)");
    expect(tileStyle("0.3").backgroundColor).toBe("rgb(var(--gain) / 0.21)");
    expect(tileStyle("-9").backgroundColor).toBe("rgb(var(--loss) / 0.80)");
    expect(tileStyle("0").backgroundColor).toContain("--border");
  });
});
