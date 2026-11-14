import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import { liveAuditCsvUrl, liveAuditQuery } from "@/api/hooks/live";
import { isLiveReason, isTotpMissing } from "@/lib/live";

describe("live hooks helpers", () => {
  it("builds the audit query and the CSV URL from the set filters only", () => {
    expect(liveAuditCsvUrl({ link_id: "", portfolio_id: "", action: "" })).toBe("/api/live-audit.csv");
    expect(liveAuditQuery({ link_id: "l1", portfolio_id: "", action: "armed" }).toString()).toBe("link_id=l1&action=armed");
  });

  it("tells a missing admin TOTP from a wrong code (both FORBIDDEN on the totp field)", () => {
    const missing = new ApiError(403, "FORBIDDEN", "Attiva prima l'autenticazione a due fattori", { field: "totp" });
    const wrong = new ApiError(403, "FORBIDDEN", "codice TOTP non valido", { field: "totp" });
    expect(isTotpMissing(missing)).toBe(true);
    expect(isTotpMissing(wrong)).toBe(false);
    expect(isTotpMissing(new Error("x"))).toBe(false);
  });

  it("recognises a reduce-only set by a real-money anomaly", () => {
    expect(isLiveReason("live_reconcile")).toBe(true);
    expect(isLiveReason("daily_loss")).toBe(false);
    expect(isLiveReason(null)).toBe(false);
  });
});
