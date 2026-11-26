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

const okSection = (label: string, summary: string) => ({ status: "ok", label, summary, reasons: [] });

/** GET /api/system-health with every section ok. */
export const HEALTH_OK = {
  status: "ok",
  reasons: [],
  checked_at: "2026-11-19T10:00:00Z",
  version: { build: "abc123", built_at: null, api_started_at: "2026-11-19T08:00:00Z", worker_started_at: "2026-11-19T08:00:05Z" },
  sections: {
    services: { ...okSection("Servizi", "Tutti i servizi rispondono"), heartbeat_age_s: 3, jobs: [] },
    providers: { ...okSection("Fornitori di dati", "1 fornitori attivi, tutti ok"), providers: [] },
    quotes: { ...okSection("Quotazioni", "Nessun simbolo in posizioni o liste"), classes: [] },
    paperclip: { ...okSection("Lettura di Paperclip", "Lettura regolare"), reader: { enabled: true } },
    cycles: { ...okSection("Cicli agenti di oggi", "0 cicli partiti su 0 attesi finora (0 nella giornata)") },
    watchdog: { ...okSection("Passaggi mancanti", "Nessun passaggio mancante oggi"), warnings: [] },
    telegram: { ...okSection("Telegram", "0 in coda, nessun errore"), pending: 0, failed_24h: 0 },
    costs: { ...okSection("Costi agenti", "Oggi 0 $, proiezione del mese 0 $"), today_usd: "0" },
    mcp: { ...okSection("Errori MCP", "Nessuna chiamata nell'ultima ora"), calls: 0, errors: 0, internal: 0, by_code: [] },
    price_quality: {
      ...okSection("Qualità dei prezzi", "Nessuna quotazione sospetta nelle 24 ore"),
      suspects_1h: 0, suspects_24h: 0, top_symbols: [], by_reason: [], by_provider: [], current: [],
    },
    quotas: { ...okSection("Quota delle fonti", "0 fornitori entro la quota"), providers: [], note: "da verificare" },
  },
};

/** Requests every authenticated page makes (Layout, halt, mode and Paperclip banners): mocked once for all page tests. */
export const commonHandlers = [
  // «Mercati v2» (plan 30): nothing unless a test overrides it
  http.get("*/api/markets/overview", () => HttpResponse.json({ as_of: "2026-11-21T12:00:00Z", ttl_s: 300, stale: false, items: [] })),
  http.get("*/api/markets/favorites", () => HttpResponse.json({ items: [] })),
  http.get("*/api/instruments/:symbol/fundamentals", ({ params }) =>
    HttpResponse.json({
      symbol: params.symbol, market_cap: null, pe_trailing: null, pe_forward: null, dividend_yield: null, low_52w: null, high_52w: null, beta: null,
      next_earnings: null, earnings_estimate: null, ex_dividend: null, dividend_date: null, currency: null, source: null,
    }),
  ),
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
    HttpResponse.json({ base_url: null, company_id: null, enabled: false, board_key_masked: null, agent_map: {}, monthly_budget_usd: null, error: null }),
  ),
  // Dashboard «Obiettivi»: no active goal unless a test overrides it
  http.get("*/api/goals/progress", () => HttpResponse.json([])),
  // «Obiettivi»: no piano ended yet unless a test overrides it
  http.get("*/api/goals/plans", () => HttpResponse.json([])),
  // «Andamento» (Dashboard, portfolio detail): no snapshot unless a test overrides it
  http.get("*/api/portfolios/equity-history", ({ request }) =>
    HttpResponse.json({
      range: new URL(request.url).searchParams.get("range") ?? "1m", start: null, end: "2026-11-16T12:00:00Z", goal_pct: null, portfolios: [], goals: [],
    }),
  ),
  // Dashboard «Posizioni aperte» / «Segnali più vicini»: empty unless a test overrides them
  http.get("*/api/positions/open", () => HttpResponse.json({ as_of: "2026-11-16T12:00:00Z", positions: [], portfolios: [] })),
  http.get("*/api/intraday/triggers", () => HttpResponse.json([])),
  http.get("*/api/intraday/accounts", () => HttpResponse.json([])),
  http.get("*/api/signals", () => HttpResponse.json({ as_of: "2026-11-16T12:00:00Z", portfolios: [] })),
  // «Impostazioni → Utenti» (admins): an empty list unless a test overrides it
  http.get("*/api/users", () => HttpResponse.json([])),
  // Dashboard «Salute del sistema» badge: everything ok unless a test overrides it
  http.get("*/api/system-health", () => HttpResponse.json(HEALTH_OK)),
  // link flows and the portfolio «Commissioni»: the brokers' fee presets
  http.get("*/api/brokers/fee-presets", () => HttpResponse.json(FEE_PRESETS)),
];
