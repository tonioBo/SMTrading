import { http, HttpResponse } from "msw";
import type { BrokerFeePreset } from "@/api/types";

const preset = (broker_kind: string, broker: string, fx_pct: string): BrokerFeePreset => ({
  broker_kind, broker, pct: "0", fixed: "0", fx_pct, note: "", source_note: "listino pubblico, da verificare",
  rule_name_suffix: broker,
});

/** GET /api/brokers/fee-presets (the backend table fees/broker_presets.py). */
export const FEE_PRESETS: BrokerFeePreset[] = [
  preset("alpaca_paper", "Alpaca", "0"),
  preset("alpaca_live", "Alpaca", "0"),
  preset("t212_demo", "Trading 212", "0.15"),
  preset("t212_live", "Trading 212", "0.15"),
  preset("etoro_demo", "eToro", "0.5"),
  preset("etoro_live", "eToro", "0.5"),
];

/** Requests every authenticated page makes (Layout, halt, mode and Paperclip banners): mocked once for all page tests. */
export const commonHandlers = [
  http.get("*/api/dashboard/modes", () => HttpResponse.json({ simulazione: 1, paper: 0, reale: 0 })),
  http.get("*/api/halts", () => HttpResponse.json({ active: [], history: [] })),
  http.get("*/api/live/status", () => HttpResponse.json({ armed_count: 0, dry_run_count: 0, portfolios: [], telegram_ok: true })),
  // Paperclip banner (Layout): reader off unless a test overrides it
  http.get("*/api/paperclip/status", () =>
    HttpResponse.json({
      enabled: false, isolation: "off", changes: { removed: [], changed: [], added: [] }, last_ok_at: null, last_error: null,
      consecutive_errors: 0, baseline_at: null, rebaseline_reason: null, baseline_count: 0, key_expires_at: null, key_expiring: false,
    }),
  ),
  http.get("*/api/paperclip/config", () =>
    HttpResponse.json({ base_url: null, company_id: null, enabled: false, board_key_masked: null, agent_map: {}, error: null }),
  ),
  // Dashboard «Obiettivo settimanale»: no data this week unless a test overrides it
  http.get("*/api/weekly-goal", () =>
    HttpResponse.json({
      week_start: "2026-11-16", on: true, target_pct: "2.0",
      total: { start_eur: null, current_eur: null, target_eur: null, pct: null }, portfolios: [],
    }),
  ),
  // «Impostazioni → Utenti» (admins): an empty list unless a test overrides it
  http.get("*/api/users", () => HttpResponse.json([])),
  // link flows and the portfolio «Commissioni»: the brokers' fee presets
  http.get("*/api/brokers/fee-presets", () => HttpResponse.json(FEE_PRESETS)),
];
