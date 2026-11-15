import { http, HttpResponse } from "msw";

/** Requests every authenticated page makes (Layout, halt and mode banners): mocked once for all page tests. */
export const commonHandlers = [
  http.get("*/api/dashboard/modes", () => HttpResponse.json({ simulazione: 1, paper: 0, reale: 0 })),
  http.get("*/api/halts", () => HttpResponse.json({ active: [], history: [] })),
  http.get("*/api/live/status", () => HttpResponse.json({ armed_count: 0, dry_run_count: 0, portfolios: [], telegram_ok: true })),
  // «Impostazioni → Utenti» (admins): an empty list unless a test overrides it
  http.get("*/api/users", () => HttpResponse.json([])),
];
