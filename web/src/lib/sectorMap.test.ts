import { describe, expect, it } from "vitest";
import type { SectorTile } from "@/api/marketsTypes";
import { readPalette } from "@/lib/chartTheme";
import { changeColor, memberNodes, sectorNodes, textOn, tooltipHtml } from "@/lib/sectorMap";

const p = readPalette(); // the dark fallback in jsdom: gain 16 185 129, loss 244 63 94
const m = (symbol: string, change_pct: string) => ({ symbol, name: `${symbol} Inc.`, sector: "Tecnologia", currency: "USD", last: "10", change_pct });

describe("changeColor", () => {
  it("diverges red → grey → green, full colour from ±3%", () => {
    const grey = changeColor(0, p);
    expect(changeColor(null, p)).toBe(grey);
    expect(changeColor(3, p)).toBe("rgb(16, 185, 129)");
    expect(changeColor(9, p)).toBe("rgb(16, 185, 129)");
    expect(changeColor(-3, p)).toBe("rgb(244, 63, 94)");
    const half = changeColor(1.5, p);
    expect(half).not.toBe(grey);
    expect(half).not.toBe(changeColor(3, p));
  });
  it("labels stay readable", () => {
    expect(textOn("rgb(250, 250, 250)")).toBe("rgb(9, 9, 11)");
    expect(textOn("rgb(20, 20, 20)")).toBe("rgb(250, 250, 250)");
  });
});

describe("treemap nodes", () => {
  const tiles: SectorTile[] = [
    { sector: "Tecnologia", count: 2, avg_change_pct: "1.5", up: 2, down: 0, members: [m("NVDA", "2"), m("AAPL", "1")] },
    { sector: "Vuoto", count: 0, avg_change_pct: "0", up: 0, down: 0, members: [] },
    { sector: "Energia & <Co>", count: 1, avg_change_pct: "-3.1", up: 0, down: 1, members: [m("XOM", "-3.1")] },
  ];
  it("one tile per sector sized by its member count, with its counts", () => {
    const n = sectorNodes(tiles, p);
    expect(n.map((x) => [x.id, x.value, x.change])).toEqual([
      ["Tecnologia", 2, 1.5],
      ["Energia & <Co>", 1, -3.1],
    ]);
    expect(n[1].color).toBe("rgb(244, 63, 94)");
    expect(tooltipHtml(n[0])).toBe("<b>Tecnologia</b><br/>media +1,50%<br/>2 titoli · ▲2 ▼0");
    expect(tooltipHtml(n[1])).toContain("Energia &#38; &#60;Co&#62;"); // escaped
  });
  it("the members of a sector: equal tiles, symbol and full name", () => {
    const n = memberNodes(tiles[0].members, p);
    expect(n.map((x) => [x.id, x.value, x.change])).toEqual([
      ["NVDA", 1, 2],
      ["AAPL", 1, 1],
    ]);
    expect(tooltipHtml(n[0])).toBe("<b>NVDA</b> · NVDA Inc.<br/>+2,00%");
  });
});
