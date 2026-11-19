import { afterEach, describe, expect, it } from "vitest";
import { crosshairLabel, cssColor, FALLBACK, pricePlaces, priceLabel, readPalette, rgbOf, tickLabel, volumeLabel, withAlpha } from "@/lib/chartTheme";

afterEach(() => document.documentElement.removeAttribute("style"));

describe("palette", () => {
  it("reads the theme tokens of :root as rgb() colours, with the dark theme as fallback", () => {
    document.documentElement.style.setProperty("--gain", "5 150 105");
    const p = readPalette();
    expect(p.gain).toBe("rgb(5, 150, 105)");
    expect(p.loss).toBe(`rgb(${FALLBACK.loss.split(" ").join(", ")})`);
  });
  it("resolves the token colours of the series for the canvas", () => {
    const p = readPalette();
    expect(cssColor("rgb(var(--loss))", p)).toBe(p.loss);
    expect(cssColor("#3b82f6", p)).toBe("#3b82f6");
    expect(cssColor("rgb(var(--nope))", p)).toBe("rgb(var(--nope))");
  });
  it("adds an alpha and reads the channels", () => {
    expect(withAlpha("rgb(1, 2, 3)", 0.5)).toBe("rgba(1, 2, 3, 0.5)");
    expect(withAlpha("#10b981", 0)).toBe("rgba(16, 185, 129, 0)");
    expect(rgbOf("#ff0001")).toEqual([255, 0, 1]);
    expect(rgbOf("rgba(4, 5, 6, 0.2)")).toEqual([4, 5, 6]);
    expect(rgbOf("red")).toBeNull();
  });
});

describe("labels", () => {
  const t = Date.parse("2026-11-16T08:05:00Z") / 1000;
  it("time axis in Europe/Rome by tick kind", () => {
    expect(tickLabel(t, 0)).toBe("2026");
    expect(tickLabel(t, 1)).toBe("nov");
    expect(tickLabel(t, 2)).toBe("16/11");
    expect(tickLabel(t, 3)).toBe("09:05");
    expect(crosshairLabel(t, false)).toBe("16/11/26 09:05");
    expect(crosshairLabel(t, true)).toBe("16/11/26");
  });
  it("prices and volumes the Italian way", () => {
    expect(priceLabel(1234.5)).toBe("1.234,50");
    expect(priceLabel(0.123456)).toBe("0,1235");
    expect(pricePlaces(0.0001234)).toBe(6);
    expect(volumeLabel(950)).toBe("950");
    expect(volumeLabel(12_300)).toBe("12,3 mila");
    expect(volumeLabel(4_500_000)).toBe("4,5 mln");
    expect(volumeLabel(2_100_000_000)).toBe("2,1 mld");
  });
});
