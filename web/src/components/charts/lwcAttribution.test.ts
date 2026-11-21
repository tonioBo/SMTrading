import { describe, expect, it } from "vitest";
import { themeOptions } from "./lwc";

describe("chart attribution", () => {
  it("hides the logo inside the charts (credited in the portal footer)", () => {
    const o = themeOptions({ muted: "#888", border: "#ddd" } as never);
    expect(o.layout?.attributionLogo).toBe(false);
  });
});
