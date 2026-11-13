import { afterEach, describe, expect, it } from "vitest";
import { cssColor } from "./cssColor";

afterEach(() => document.documentElement.style.removeProperty("--accent"));

describe("cssColor", () => {
  it("turns the space-separated token into comma rgb()", () => {
    document.documentElement.style.setProperty("--accent", " 16 185  129 ");
    expect(cssColor("--accent", "#000")).toBe("rgb(16, 185, 129)");
  });

  it("falls back when the variable is not set", () => {
    expect(cssColor("--accent", "#10b981")).toBe("#10b981");
  });
});
