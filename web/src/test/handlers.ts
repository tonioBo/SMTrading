import { http, HttpResponse } from "msw";

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
  // «Impostazioni → Utenti» (admins): an empty list unless a test overrides it
  http.get("*/api/users", () => HttpResponse.json([])),
];
