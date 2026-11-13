import { describe, expect, it } from "vitest";
import { zonedDayEnd, zonedDayStart, zonedIso } from "./zonedDate";

describe("zonedIso", () => {
  it("uses the portfolio timezone offset, not the browser's", () => {
    expect(zonedDayStart("2026-11-12", "Europe/Rome")).toBe("2026-11-12T00:00:00+01:00");
    expect(zonedDayEnd("2026-07-01", "Europe/Rome")).toBe("2026-07-01T23:59:59.999+02:00");
    expect(zonedDayStart("2026-11-12", "America/New_York")).toBe("2026-11-12T00:00:00-05:00");
    expect(zonedDayStart("2026-11-12", "UTC")).toBe("2026-11-12T00:00:00+00:00");
  });

  it("handles the DST change days", () => {
    // Rome: 29 Mar 2026 02:00 → 03:00; 25 Oct 2026 03:00 → 02:00
    expect(zonedDayStart("2026-03-29", "Europe/Rome")).toBe("2026-03-29T00:00:00+01:00");
    expect(zonedDayEnd("2026-03-29", "Europe/Rome")).toBe("2026-03-29T23:59:59.999+02:00");
    expect(zonedDayStart("2026-10-25", "Europe/Rome")).toBe("2026-10-25T00:00:00+02:00");
    expect(zonedDayEnd("2026-10-25", "Europe/Rome")).toBe("2026-10-25T23:59:59.999+01:00");
    expect(zonedIso("2026-11-12", "12:30:00", "Asia/Kolkata")).toBe("2026-11-12T12:30:00+05:30");
  });
});
