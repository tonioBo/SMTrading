import { describe, expect, it } from "vitest";
import { formatClock, formatDateTime, formatHolding, formatMoney, formatPct, formatQty, formatSignedMoney, signTone } from "./format";

describe("formatMoney", () => {
  it("formats EUR in it-IT with grouping and 2 decimals", () => {
    expect(formatMoney("1234.5", "EUR")).toBe("1.234,50 €");
    expect(formatMoney("1234567.891", "EUR")).toBe("1.234.567,89 €");
    expect(formatMoney("0", "EUR")).toBe("0,00 €");
    expect(formatMoney("12", "EUR")).toBe("12,00 €");
  });

  it("rounds HALF_UP on the string digits, never via float", () => {
    expect(formatMoney("0.005", "EUR")).toBe("0,01 €");
    expect(formatMoney("1.005", "EUR")).toBe("1,01 €"); // float would give 1,00
    expect(formatMoney("-1.005", "EUR")).toBe("-1,01 €");
    expect(formatMoney("999.995", "EUR")).toBe("1.000,00 €");
    expect(formatMoney("12345678901234567890.125", "EUR")).toBe(
      "12.345.678.901.234.567.890,13 €",
    );
  });

  it("never shows a negative zero", () => {
    expect(formatMoney("-0.004", "EUR")).toBe("0,00 €");
    expect(formatMoney("-0", "EUR")).toBe("0,00 €");
  });

  it("keeps the sign of negative amounts", () => {
    expect(formatMoney("-1234.5", "EUR")).toBe("-1.234,50 €");
  });

  it("uses the currency's minor units and symbol", () => {
    expect(formatMoney("1234.5", "JPY")).toBe("1.235 ¥");
    expect(formatMoney("10.1", "USD")).toBe("10,10 $");
    expect(formatMoney("10.1", "CHF")).toBe("10,10 CHF");
  });

  it("returns non-decimal input unchanged", () => {
    expect(formatMoney("abc", "EUR")).toBe("abc");
  });
});

describe("formatQty", () => {
  it("keeps up to 8 decimals and strips trailing zeros", () => {
    expect(formatQty("0.12345678000")).toBe("0,12345678");
    expect(formatQty("10.50")).toBe("10,5");
    expect(formatQty("1500")).toBe("1.500");
    expect(formatQty("0.123456789")).toBe("0,12345679");
    expect(formatQty("-2.000")).toBe("-2");
    expect(formatQty("-0.000000001")).toBe("0");
  });
});

describe("formatPct", () => {
  it("adds an explicit sign and 2 decimals", () => {
    expect(formatPct("1.25")).toBe("+1,25%");
    expect(formatPct("-0.5")).toBe("-0,50%");
    expect(formatPct("0")).toBe("0,00%");
    expect(formatPct("1234.567")).toBe("+1.234,57%");
  });
});

describe("formatDateTime", () => {
  it("formats in Europe/Rome as dd/MM/yyyy HH:mm:ss", () => {
    expect(formatDateTime("2026-01-15T08:05:09Z")).toBe("15/01/2026 09:05:09"); // CET
    expect(formatDateTime("2026-07-01T22:30:00Z")).toBe("02/07/2026 00:30:00"); // CEST
  });

  it("returns invalid input unchanged", () => {
    expect(formatDateTime("not a date")).toBe("not a date");
  });
});

describe("formatSignedMoney", () => {
  it("adds + to gains, keeps - on losses, no sign on zero", () => {
    expect(formatSignedMoney("12.5", "EUR")).toBe("+12,50 €");
    expect(formatSignedMoney("-1234.567", "EUR")).toBe("-1.234,57 €");
    expect(formatSignedMoney("0", "EUR")).toBe("0,00 €");
    expect(formatSignedMoney("-0.001", "EUR")).toBe("0,00 €");
    expect(formatSignedMoney("3", "USD")).toBe("+3,00 $");
    expect(formatSignedMoney("n/d", "EUR")).toBe("n/d");
  });
});

describe("signTone", () => {
  it("gain at or above zero, loss below, by the rounded value; null when unknown", () => {
    expect(signTone("1.2")).toBe("gain");
    expect(signTone("0")).toBe("gain");
    expect(signTone("-0.001")).toBe("gain"); // shown as 0,00
    expect(signTone("-0.01")).toBe("loss");
    expect(signTone("-5", 0)).toBe("loss");
    expect(signTone("-0.4", 0)).toBe("gain");
    expect(signTone(null)).toBeNull();
    expect(signTone("abc")).toBeNull();
  });
});

describe("formatClock", () => {
  it("shows the Rome time today, the day too otherwise", () => {
    const now = new Date("2026-11-17T22:00:00Z");
    expect(formatClock("2026-11-17T21:45:00Z", now)).toBe("22:45");
    expect(formatClock("2026-11-14T00:30:00Z", now)).toBe("14/11 01:30");
    expect(formatClock("nope", now)).toBe("nope");
  });
});

describe("formatHolding", () => {
  it("shows days and hours, hours and minutes, or minutes", () => {
    expect(formatHolding(null)).toBe("—");
    expect(formatHolding(30)).toBe("< 1 min");
    expect(formatHolding(45 * 60)).toBe("45 min");
    expect(formatHolding(5400)).toBe("1 h 30 min");
    expect(formatHolding(7200)).toBe("2 h");
    expect(formatHolding(2 * 86400 + 7 * 3600 + 120)).toBe("2 g 7 h");
    expect(formatHolding(86400)).toBe("1 g");
  });
});
