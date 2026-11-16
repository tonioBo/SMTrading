import { http, HttpResponse } from "msw";
import type { AssetClass, BrokerFeePreset, BrokerFeePresetEntry } from "@/api/types";

const entry = (asset_class: AssetClass | null, pct: string, fx_pct: string, source_note: string): BrokerFeePresetEntry => ({
  asset_class, pct, fixed: "0", fx_pct, note: "", source_note,
});
const ALPACA = [entry(null, "0", "0", "listino pubblico, da verificare"), entry("crypto", "0.25", "0", "fonte non ufficiale, da verificare")];
const T212 = [entry(null, "0", "0.15", "più fonti concordi")];
const ETORO_NOTE = "guida giugno 2026; fonti discordanti 0,5–1%, da verificare";
const ETORO = [entry(null, "0", "0.75", ETORO_NOTE), entry("crypto", "1", "0.75", ETORO_NOTE)];

/** GET /api/brokers/fee-presets (the backend table fees/broker_presets.py). */
export const FEE_PRESETS: BrokerFeePreset[] = [
  { broker_kind: "alpaca_paper", broker: "Alpaca", entries: ALPACA },
  { broker_kind: "alpaca_live", broker: "Alpaca", entries: ALPACA },
  { broker_kind: "t212_demo", broker: "Trading 212", entries: T212 },
  { broker_kind: "t212_live", broker: "Trading 212", entries: T212 },
  { broker_kind: "etoro_demo", broker: "eToro", entries: ETORO },
  { broker_kind: "etoro_live", broker: "eToro", entries: ETORO },
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
