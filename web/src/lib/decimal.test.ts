import { describe, expect, it } from "vitest";
import { normalizeAmount, subtractDecimal } from "@/lib/decimal";

describe("subtractDecimal", () => {
  it("subtracts plain decimal strings exactly", () => {
    expect(subtractDecimal("1000.5", "250.25")).toBe("750.25");
    expect(subtractDecimal("0.1", "0.3")).toBe("-0.2");
    expect(subtractDecimal("10", "10.00")).toBe("0");
    expect(subtractDecimal("-5", "2.5")).toBe("-7.5");
  });

  it("returns null for non-decimal input", () => {
    expect(subtractDecimal("abc", "1")).toBeNull();
  });
});

describe("normalizeAmount", () => {
  it("accepts it-IT and plain input", () => {
    expect(normalizeAmount("1.234,56")).toBe("1234.56");
    expect(normalizeAmount(" 1000,5 ")).toBe("1000.5");
    expect(normalizeAmount("1000.50")).toBe("1000.50");
    expect(normalizeAmount("0")).toBe("0");
  });

  it("rejects invalid or negative amounts", () => {
    expect(normalizeAmount("")).toBeNull();
    expect(normalizeAmount("-5")).toBeNull();
    expect(normalizeAmount("1,2,3")).toBeNull();
    expect(normalizeAmount("12a")).toBeNull();
  });
});
