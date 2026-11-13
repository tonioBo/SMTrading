import { describe, expect, it } from "vitest";
import { AMBIGUOUS_AMOUNT_MESSAGE, amountError, normalizeAmount, subtractDecimal, toInputDecimal } from "@/lib/decimal";

describe("subtractDecimal", () => {
  it("subtracts plain decimal strings exactly", () => {
    expect(subtractDecimal("1000.5", "250.25")).toBe("750.25");
    expect(subtractDecimal("0.1", "0.3")).toBe("-0.2");
    expect(subtractDecimal("10", "10.00")).toBe("0");
    expect(subtractDecimal("-5", "2.5")).toBe("-7.5");
  });

  it("ignores surrounding spaces", () => {
    expect(subtractDecimal(" 1.25 ", "1 ")).toBe("0.25");
  });

  it("returns null for non-decimal input", () => {
    expect(subtractDecimal("abc", "1")).toBeNull();
  });
});

describe("normalizeAmount (it-IT)", () => {
  it.each([
    ["10.000,50", "10000.50"],
    ["1,5", "1.5"],
    ["1.5", "1.5"],
    ["1.000.000,00", "1000000.00"],
    ["1000,5", "1000.5"],
    ["1000.50", "1000.50"],
    ["0.125", "0.125"],
    ["0", "0"],
    [" 2 500,00 ", "2500.00"],
  ])("%s → %s", (input, expected) => {
    expect(normalizeAmount(input)).toBe(expected);
  });

  it.each(["10.000", "1.000.000", "999.999"])("refuses the ambiguous %s", (input) => {
    expect(normalizeAmount(input)).toBeNull();
    expect(amountError(input)).toBe(AMBIGUOUS_AMOUNT_MESSAGE);
  });

  it.each(["", "-5", "1,2,3", "12a", "10.00,5", "1.0000,5", ",5", "1,"])("rejects %j", (input) => {
    expect(normalizeAmount(input)).toBeNull();
    expect(amountError(input)).toBe("Importo non valido");
  });
});

describe("toInputDecimal", () => {
  it("turns API decimals into it-IT input that normalizes back unchanged", () => {
    for (const v of ["1.125", "10000", "0.5", "1000000.25"]) expect(normalizeAmount(toInputDecimal(v))).toBe(v);
    expect(toInputDecimal("1.125")).toBe("1,125");
    expect(toInputDecimal(null)).toBe("");
  });
});
