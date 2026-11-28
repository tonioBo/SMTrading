/** Admin API payloads. Decimals are strings on the wire. */

export type UserRole = "admin" | "viewer";

/** The logged-in user (/api/auth/me). `role` and `must_change_password` may be missing
 * from an older server: treated as an admin without a password to change. */
export interface AdminUser {
  email: string;
  totp_enabled: boolean;
  role?: UserRole;
  must_change_password?: boolean;
}

/** A portal user in «Impostazioni → Utenti» (/api/users). */
export interface PortalUser {
  id: string;
  email: string;
  role: UserRole;
  totp_enabled: boolean;
  must_change_password: boolean;
  active: boolean;
  disabled_at: string | null;
  last_login_at: string | null;
  created_at: string;
  created_by: string | null;
}

/** Create and password-reset answers: the temporary password is shown once. */
export interface PortalUserWithPassword {
  user: PortalUser;
  temp_password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
  totp?: string;
}

export interface KillSwitchState {
  on: boolean;
}

/** «Blocco aperture»: while active, orders that would increase exposure are refused. */
export interface TradingHalt {
  id: number;
  scope: "global" | "portfolio";
  portfolio_id: string | null;
  portfolio: string | null;
  active: boolean;
  reason: string;
  /** "agent:<id>" or "admin:<email>" */
  set_by: string;
  /** the agent's name or the admin email */
  set_by_name: string | null;
  set_at: string;
  cleared_by: string | null;
  cleared_by_name: string | null;
  cleared_at: string | null;
}

export interface TradingHalts {
  active: TradingHalt[];
  /** the last 50 cleared, most recently cleared first */
  history: TradingHalt[];
}

export interface TradingHaltInput {
  scope: "global" | "portfolio";
  portfolio_id?: string | null;
  reason: string;
}

export interface KillSwitchResult extends KillSwitchState {
  canceled: number;
}

/** simulazione = no broker (virtual money); paper = demo broker account; reale = a real-money link that is armed */
export type PortfolioMode = "simulazione" | "paper" | "reale";

/** State of a real-money («denaro reale») link; null for no link or a paper/demo link. */
export type LiveState = "configured" | "dry_run" | "armed";

export interface DashboardPortfolio {
  id: string;
  name: string;
  status: string;
  mode: PortfolioMode;
  live_state?: LiveState | null;
  /** the link's kind (alpaca_paper, t212_live…), null without a broker */
  broker_kind: string | null;
  /** the mode with the broker's name: «SMTrading (simulato)», «Alpaca · prova», «🔴 Trading 212 · reale»… */
  mode_label: string;
  equity_eur: string | null;
  day_pnl_eur: string | null;
  day_pnl_pct: string | null;
  stale_symbols: string[];
  error: string | null;
}

export interface ModeCount {
  label: string;
  count: number;
  /** a real-money link (any live_state) */
  live: boolean;
}

export interface DashboardConnector {
  name: string;
  enabled: boolean;
  health_status: string; // ok | error | unknown
  last_checked_at: string | null;
}

export interface MarginAlert {
  id: number;
  portfolio_id: string | null;
  kind: string; // margin_warning | liquidation
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Dashboard {
  /** every non-archived portfolio, also those out of the goals */
  total_equity_eur: string;
  /** the equity of the portfolios counted in the goals (`in_totals`) */
  goals_equity_eur?: string;
  /** the portfolios left out of the goals (`in_totals` false); equity null when not valued */
  excluded_from_goals?: { id: string; name: string; equity_eur: string | null }[];
  day_pnl_eur: string;
  day_pnl_pct: string | null;
  portfolios: DashboardPortfolio[];
  /** non-archived portfolios per mode */
  modes: Record<PortfolioMode, number>;
  /** non-archived portfolios per mode_label: simulated, then practice/demo, then real money */
  mode_counts: ModeCount[];
  connectors: DashboardConnector[];
  margin_alerts: MarginAlert[];
  kill_switch: boolean;
  worker_heartbeat_age_s: number | null;
  /** symbols held by the live portfolios: their quotes drive the real-time refresh */
  held_symbols?: string[];
}

export type EquityPeriod = "1d" | "1w" | "1m" | "3m" | "1y" | "all";

export interface EquityPoint {
  ts: string;
  equity_eur: string;
}

export type PortfolioStatus = "active" | "reduce_only" | "locked" | "archived";

export interface Portfolio {
  id: string;
  name: string;
  /** the agents' short code (2–6 A-Z/0-9, unique among the live portfolios), null: none */
  code?: string | null;
  base_currency: string;
  leverage: string;
  maintenance_margin_pct: string;
  warning_margin_pct: string;
  slippage_bps: number;
  status: PortfolioStatus;
  mode: PortfolioMode;
  /** the real-money link's state (configured | dry_run | armed), null otherwise */
  live_state?: LiveState | null;
  /** the link's kind (alpaca_paper, t212_live…), null without a broker */
  broker_kind: string | null;
  /** the mode with the broker's name (see DashboardPortfolio.mode_label) */
  mode_label: string;
  timezone: string;
  risk_profile_id: string | null;
  reduce_only_reason: string | null;
  reduce_only_until: string | null;
  created_at: string;
  equity_eur: string | null;
  day_pnl_eur: string | null;
  day_pnl_pct: string | null;
  /** error code when the portfolio could not be valued (equity/P&L are then null) */
  valuation_error: string | null;
  /** the broker link (paper/demo account) the portfolio's orders are routed to */
  broker_link_id?: string | null;
  /** set while archived: hidden, read-only, skipped by every job */
  archived_at?: string | null;
  archived_by?: string | null;
  /** false: left out of the totals and of the total goals / piano (`portfolios.in_totals`) */
  in_totals?: boolean;
  /** only in a save response: set when the name has a word the agents should not see */
  name_warning?: string | null;
  /** intraday mode A (agent wake on a signal); off by default */
  intraday_enabled?: boolean;
  /** intraday mode B (automatic rules, trials); off by default, never with mode A */
  auto_rules_enabled?: boolean;
  /** "5m" | "15m" | "1h" */
  intraday_timeframes?: string[];
  /** the agent woken by a trigger, null: nobody (logged and notified only) */
  intraday_wake_agent_id?: string | null;
  day_close_minutes?: number;
  /** day-trade cap over 5 sessions, null: no cap */
  max_day_trades_5d?: number | null;
  intraday_max_wakes_per_hour?: number;
  /** brakes of the automatic rules, per portfolio (null = brake off) */
  auto_brakes?: Record<string, string | number | null>;
}

export interface PortfolioCreate {
  name: string;
  initial_cash: string;
  base_currency: string;
  leverage: string;
  slippage_bps: number;
  timezone: string;
  risk_profile_id: string | null;
  code?: string | null;
}

export interface PortfolioPatch {
  status?: "active" | "locked";
  name?: string;
  leverage?: string;
  maintenance_margin_pct?: string;
  warning_margin_pct?: string;
  slippage_bps?: number;
  risk_profile_id?: string | null;
  timezone?: string;
  in_totals?: boolean;
  /** empty / null clears it */
  code?: string | null;
  intraday_enabled?: boolean;
  auto_rules_enabled?: boolean;
  intraday_timeframes?: string[];
  intraday_wake_agent_id?: string | null;
  day_close_minutes?: number;
  max_day_trades_5d?: number | null;
  intraday_max_wakes_per_hour?: number;
  auto_brakes?: Record<string, string | number | null>;
}

export interface PositionValuation {
  symbol: string;
  qty: string;
  avg_price: string;
  mark: string;
  currency: string;
  market_value: string;
  market_value_eur: string;
  unrealized_pnl: string;
  unrealized_pnl_eur: string;
  stale: boolean;
}

export interface Position extends PositionValuation {
  side: "long" | "short";
}

export type MarginLevel = "ok" | "warning" | "maintenance";

export interface Valuation {
  portfolio_id: string;
  base_currency: string;
  cash: Record<string, string>;
  collateral: Record<string, string>;
  margin_loan: Record<string, string>;
  equity_eur: string;
  cash_eur: string;
  exposure_eur: string;
  initial_req_eur: string;
  maint_req_eur: string;
  warn_req_eur: string;
  buying_power_eur: string;
  stale_symbols: string[];
  ts: string;
  margin_level: MarginLevel;
  positions: PositionValuation[];
}

export interface Performance {
  series: EquityPoint[];
  return_pct: string | null;
  max_drawdown_pct: string | null;
  sharpe: string | null;
}

export type OrderStatus =
  | "new"
  | "held"
  | "queued"
  | "pending"
  | "triggered"
  | "partially_filled"
  | "filled"
  | "canceled"
  | "rejected"
  | "expired";

export interface Order {
  id: string;
  portfolio_id: string;
  agent_id: string | null;
  client_order_id: string | null;
  symbol: string;
  side: "buy" | "sell";
  type: string;
  tif: string;
  qty: string | null;
  notional: string | null;
  limit_price: string | null;
  stop_price: string | null;
  filled_qty: string;
  avg_fill_price: string | null;
  status: OrderStatus;
  reduce_only: boolean;
  /** good-till-time of the order (ISO): unfilled then, it is canceled and becomes expired */
  valid_until?: string | null;
  reject_code: string | null;
  reject_message: string | null;
  source: string;
  created_at: string;
  updated_at: string;
  /** set when the order was routed to the portfolio's broker */
  broker_link_id?: string | null;
  /** true once the broker has the order */
  broker_replicated?: boolean;
  /** to_send, sending, unknown, cancel_requested, replace_requested or the broker's state */
  broker_status?: string | null;
  broker_order_id?: string | null;
  /** opened as a day trade (intraday) */
  day_trade?: boolean;
}

export type OrderType = "market" | "limit" | "stop" | "stop_limit" | "trailing_stop";
/** engine TimeInForce */
export type TimeInForce = "day" | "gtc";

/** Body of POST /api/portfolios/{id}/orders[/preview]: decimals as strings. */
export interface OrderInput {
  symbol: string;
  side: "buy" | "sell";
  type: OrderType;
  tif: TimeInForce;
  qty?: string;
  notional?: string;
  limit_price?: string;
  stop_price?: string;
  trail_pct?: string;
  trail_amount?: string;
  /** an ISO datetime with offset, or "close" (today's session close; not for crypto/FX) */
  valid_until?: string;
  /** only for placing on a real-money portfolio */
  totp?: string;
  /** a buy meant to be closed the same session (counted against the day-trade cap) */
  day_trade?: boolean;
}

/** POST /api/portfolios/{id}/orders/preview: a refused check is ok=false with its code. */
/** One instrument as the portfolio's broker takes it (plan 44); null: not known now. */
export interface TradabilityItem {
  symbol: string;
  tradable: boolean | null;
  long_unleveraged: boolean | null;
  short: boolean | null;
  fractional: boolean | null;
  partial_close: boolean | null;
  /** why it is not tradable (Italian), null when it is */
  reason: string | null;
}

export interface TradabilityCheck {
  /** false: no broker, the catalog answers */
  linked: boolean;
  /** the universes of the portfolio's approved strategies */
  universes: { ref: string; name: string | null; symbols: string[] }[];
  items: TradabilityItem[];
}

export interface OrderPreview {
  ok: boolean;
  code: string | null;
  message: string | null;
  /** in the instrument's currency */
  estimated_price: string | null;
  estimated_notional_eur: string | null;
  estimated_fee: { rule_id: string; amount: string; currency: string } | null;
  estimated_fx_fee_eur: string | null;
  buying_power_after_eur: string | null;
  /** the portfolio is linked to a real-money broker (dry run included) */
  real: boolean;
  /** the portfolio's base currency */
  currency: string;
}

export type JournalType =
  | "DEPOSIT"
  | "WITHDRAWAL"
  | "TRADE"
  | "FEE"
  | "FX"
  | "INTEREST"
  | "DIVIDEND"
  | "CORPORATE_ACTION"
  | "LIQUIDATION"
  | "RESET"
  | "REVERSAL";

export interface TransactionLine {
  asset: string;
  kind: string; // cash | position | collateral | margin_loan | ...
  quantity: string;
  unit_price: string | null;
  price_ccy: string | null;
}

export interface Transaction {
  id: string;
  seq: number;
  type: JournalType;
  created_at: string;
  reason: string | null;
  order_id: string | null;
  author_type: string | null;
  author_id: string | null;
  reversed_journal_id: string | null;
  /** id of the REVERSAL of this journal, if any */
  reversed_by: string | null;
  lines: TransactionLine[];
}

export interface TransactionPage {
  items: Transaction[];
  next_before_seq: number | null;
}

export interface CashMove {
  amount: string;
  currency: string;
  reason?: string;
}

export interface ResetRequest {
  new_cash: string;
  currency: string;
  reason: string;
}

export interface ResetResult {
  portfolio_id: string;
  canceled_orders: number;
  closing_orders: Order[];
  reset_journal_id: string | null;
  deposit_journal_id: string | null;
}

export interface RiskProfile {
  id: string;
  name: string;
  max_order_notional_eur: string | null;
  max_position_pct: string | null;
  max_daily_loss_pct: string | null;
  max_daily_loss_eur: string | null;
  allowed_symbols: string[];
  allowed_asset_classes: string[];
  allowed_markets: string[];
  short_allowed: boolean;
  description: string | null;
  /** fuse (Ruling R8.1): real orders sent per hour at most, 1-1000, default 30 */
  max_live_orders_per_hour?: number;
  /** safety close at the end of the session: limit margin in percent, 0 < x <= 5 (default 0.5) */
  day_close_limit_margin_pct?: string;
  /** live portfolios using it (list only; Default also counts those without a profile) */
  portfolios_count?: number;
  /** only in a save response: set when the name has a word the agents should not see */
  name_warning?: string | null;
}

export type AgentPermission = "read" | "trade";

export interface AgentPortfolioLink {
  portfolio_id: string;
  portfolio_name: string;
  permission: AgentPermission;
  archived?: boolean;
  mode?: PortfolioMode;
  live_state?: LiveState | null;
  broker_kind?: string | null;
  mode_label?: string;
}

export interface AgentWatchlist {
  id: string;
  name: string;
  symbols: string[];
  created_at: string;
}

export interface AgentAlert {
  id: string;
  symbol: string;
  condition: string; // price_above | price_below | change_pct_above | change_pct_below
  value: string;
  ref_price: string | null;
  status: string; // active | triggered
  note: string | null;
  created_at: string;
  triggered_at: string | null;
  triggered_price: string | null;
}

export interface Agent {
  id: string;
  name: string;
  active: boolean;
  created_at: string;
  last_seen_at: string | null;
  /** the `notify` permission: may send messages to the Telegram group */
  can_notify: boolean;
  /** the `halt` permission: may block new openings (set_trading_halt) */
  can_halt: boolean;
  portfolios: AgentPortfolioLink[];
}

/** The only responses that carry a token: agent creation and rotation. */
export interface AgentCreated {
  agent: Agent;
  token: string;
  /** set when the name has a word the agents should not see (the agent is created) */
  name_warning?: string | null;
}

export interface AgentRotated {
  token: string;
}

export interface AgentPortfolioAssignment {
  portfolio_id: string;
  permission: AgentPermission;
}

export interface McpAuditEntry {
  id: number;
  agent_id: string | null;
  method: string;
  params: unknown;
  outcome: string;
  latency_ms: number;
  created_at: string;
}

export interface McpAuditFilters {
  agent_id: string;
  method: string;
  outcome: string;
  since: string; // yyyy-mm-dd or ""
}

export type FeeScope = "global" | "asset_class" | "portfolio";
export type FeeSide = "buy" | "sell" | "both";

export interface FeeTier {
  up_to: string | null;
  pct: string;
}

export interface FeeRule {
  id: string;
  name: string;
  scope: FeeScope;
  scope_ref: string | null;
  /** portfolio scope only: the rule covers just this asset class (null: every class) */
  asset_class: AssetClass | null;
  side: FeeSide;
  currency: string;
  fixed: string;
  pct: string;
  min_fee: string | null;
  max_fee: string | null;
  tiers: FeeTier[] | null;
  fx_pct: string;
  priority: number;
  active: boolean;
  created_at: string;
}

export type FeeRuleInput = Omit<FeeRule, "id" | "created_at">;

/** One rule of a broker's fee preset (asset_class null: stocks/ETF and every class without its own entry). */
export interface BrokerFeePresetEntry {
  asset_class: AssetClass | null;
  pct: string;
  fixed: string;
  fx_pct: string;
  note: string;
  /** how sure the figures are («listino pubblico, da verificare»…) */
  source_note: string;
}

/** GET /api/brokers/fee-presets: the fee preset (a set of rules) of a broker kind. */
export interface BrokerFeePreset {
  broker_kind: string;
  /** «Alpaca», «Trading 212», «eToro»: the rules are named «<portafoglio> – <broker>[ · <classe>]» */
  broker: string;
  entries: BrokerFeePresetEntry[];
}

/** POST /api/portfolios/{id}/fee-rule/apply-preset */
export interface FeePresetApplyResult {
  /** one rule per preset entry */
  rules: FeeRule[];
  /** ids of the rules created now (the others were updated) */
  created: string[];
  /** the portfolio's previous portfolio-scope rules, now deactivated */
  deactivated: { id: string; name: string }[];
  preset: BrokerFeePreset;
}

export interface FeeSimulateRequest {
  portfolio_id: string;
  symbol: string;
  side: "buy" | "sell";
  qty?: string;
  notional?: string;
}

/** POST /api/fees/simulate: which rule wins for (portfolio, class, side) and its amounts. */
export interface FeeRuleSimulateRequest {
  portfolio_id: string;
  asset_class: AssetClass;
  side: "buy" | "sell";
  notional: string;
  currency: string;
}

export interface FeeRuleSimulation {
  rule_id: string;
  rule_name: string;
  scope: FeeScope;
  fee: string;
  currency: string;
  fx_fee: string;
  fx_currency: string;
}

export interface FeeSimulation {
  rule_id: string;
  rule_name: string;
  notional_eur: string | null;
  fee: string;
  fee_currency: string;
  fx_fee_eur: string | null;
}

export interface RiskProfileInput {
  name: string;
  max_order_notional_eur: string | null;
  max_position_pct: string | null;
  max_daily_loss_pct: string | null;
  max_daily_loss_eur: string | null;
  allowed_symbols: string[];
  allowed_asset_classes: string[];
  allowed_markets: string[];
  short_allowed: boolean;
  description: string | null;
  max_live_orders_per_hour?: number;
  day_close_limit_margin_pct?: string;
}

export type InterestKind = "loan" | "borrow";

export interface InterestRate {
  kind: InterestKind;
  key: string;
  annual_pct: string;
}

export type AssetClass = "us_equity" | "eu_equity" | "crypto" | "forex";

/** Weekly goal line of the Telegram summaries; `pct` a decimal string ("2.0"). */
export interface WeeklyGoal {
  on: boolean;
  pct: string;
}

/** Pre-open Telegram check, Monday to Friday; `at` local time "HH:MM". */
export interface PreopenCheck {
  on: boolean;
  at: string;
}

/** Hand-off watchdog: Telegram warnings when a hand-off between agents is missing. */
export interface HandoffWatchdog {
  on: boolean;
}

/** The weekly PDF report on Telegram; `weekday` 0 = Monday, `at` local time "HH:MM". */
export interface WeeklyReportPdf {
  on: boolean;
  weekday: number;
  at: string;
}

export interface AppSettings {
  stale_quote_s: Record<string, number>;
  default_spread_bps: Record<string, number>;
  timezone: string;
  mcp_hide_mode: boolean;
  weekly_goal?: WeeklyGoal;
  preopen_check?: PreopenCheck;
  handoff_watchdog?: HandoffWatchdog;
  /** «Sentinella di sistema» (missing on an older api) */
  system_sentinel?: { on: boolean };
  weekly_report_pdf?: WeeklyReportPdf;
}

export interface Connector {
  name: string;
  enabled: boolean;
  requires_key: boolean;
  capabilities: string[];
  asset_classes: string[];
  /** false: the provider exists in the catalog but has no implementation yet */
  implemented: boolean;
  /** secret leaves are masked ("••••abcd") */
  config_masked: Record<string, unknown>;
  /** config fields shown even when empty (besides `api_key` when `requires_key`) */
  config_fields: string[];
  /** top-level config keys holding secrets (decided by the server) */
  secret_keys: string[];
  rate_limit_per_min: number | null;
  health_status: string; // ok | error | unknown
  last_error: string | null;
  last_checked_at: string | null;
  /** budgets and usage of the provider (null: not implemented) */
  quota?: ConnectorQuota | null;
}

/** A provider's budgets (null: none) and the calls counted this minute / hour / UTC day. */
export interface ProviderQuota {
  per_min: number | null;
  per_hour: number | null;
  per_day: number | null;
  used_minute: number;
  used_hour: number;
  used_day: number;
  pct_minute: number | null;
  pct_hour: number | null;
  pct_day: number | null;
  /** tried after every other provider until the reset */
  demoted: boolean;
  demoted_reason: "429" | "day" | "hour" | null;
  demoted_until: string | null;
  /** next daily reset (00:00 UTC) */
  resets_at: string;
}

export interface ConnectorQuota extends ProviderQuota {
  defaults: { per_min: number | null; per_hour: number | null; per_day: number | null };
  /** budgets set by the admin (the others are the defaults) */
  custom: ("per_hour" | "per_day")[];
  note: string;
}

/** PATCH merge: omitted secret = kept, null = removed, unchanged masked value = kept. */
export interface ConnectorPatch {
  enabled?: boolean;
  config?: Record<string, unknown>;
  rate_limit_per_min?: number;
  /** null = back to the default, 0 = no budget */
  hourly_budget?: number | null;
  daily_budget?: number | null;
  clear_quota_block?: boolean;
}

export interface ConnectorTestResult {
  ok: boolean;
  error: string | null;
  checked_at?: string;
}

export interface ProviderPriority {
  asset_class: AssetClass;
  capability: string;
  providers: string[];
}

export type StreamState = "connected" | "connecting" | "backoff";

export interface StreamStatus {
  /** note: why the provider refuses the stream for now (e.g. Alpaca's connection limit) */
  streams: Record<string, { symbols: string[]; state: StreamState | string; note?: string | null }>;
  polling: string[];
  /** false: another worker holds the stream (this one stands by) */
  owner?: boolean;
  /** false: the worker's status is stale or absent */
  available: boolean;
  tracked: string[];
  /** symbol -> who follows it (orders, positions, alerts, watchlists, admin) */
  tracked_sources?: Record<string, TrackSource[]>;
}

export type TrackSource = "orders" | "positions" | "alerts" | "watchlists" | "admin";

export interface UntrackResponse {
  untracked: string[];
  /** symbols still followed by other sources after the admin stopped */
  still_tracked: Record<string, TrackSource[]>;
}

export type InstrumentSource = "catalog" | "manual" | "auto";

export interface InstrumentsPage {
  items: Instrument[];
  total: number;
  offset: number;
  limit: number;
}

export interface AutoAddSetting {
  on: boolean;
  daily_limit: number;
}

export interface Instrument {
  symbol: string;
  name: string;
  asset_class: AssetClass;
  market: string;
  currency: string;
  lot_size: string;
  tick_size: string;
  shortable: boolean;
  active: boolean;
  provider_symbols: Record<string, string>;
  source: InstrumentSource;
  /** "auto:<agent name>" for on-demand instruments */
  added_by: string | null;
  /** Italian sector (GICS-like, or ETF / Crypto / Cambi); null when unknown */
  sector?: string | null;
  /** the provider's industry; for ETFs the asset type */
  industry?: string | null;
  /** index codes: SP500, NDX, FTSEMIB, DAX, CAC40, MDAX */
  indices?: string[] | null;
  created_at?: string | null;
}

export interface InstrumentCreate {
  symbol: string;
  name: string;
  asset_class: AssetClass;
  market: string;
  currency: string;
  lot_size: string;
  tick_size: string;
  shortable: boolean;
}

/** US pre-market / after-hours price while the regular session is closed: information
 * only (orders, stops, valuation and P/L use the regular session). */
export interface ExtendedHours {
  session: "pre" | "post";
  price: string;
  /** percent vs the latest regular close */
  change_pct_vs_close: string;
  ts: string;
}

export interface Quote {
  symbol: string;
  bid: string | null;
  ask: string | null;
  last: string | null;
  ts: string | null;
  provider: string | null;
  /** US equities (GET /api/quotes): null while the regular session is open or no price */
  extended?: ExtendedHours | null;
}

/** GET /api/quotes: a quote per symbol, or `{error}` for a symbol that failed. */
export type QuotesResponse = Record<string, Quote | { error: string }>;

export interface MarketStatus {
  market: string;
  /** false for a market shown for its hours only (LSE): no instruments can be added. */
  tradable: boolean;
  is_open: boolean;
  next_open: string | null;
  next_close: string | null;
}

export type MarketClosureKind = "closed" | "early_close";

export interface MarketClosure {
  id: number;
  market: string;
  day: string;
  kind: MarketClosureKind;
  /** "HH:MM", local time of the exchange; early_close only. */
  close_time: string | null;
  note: string;
  created_by: string;
  created_at: string | null;
}

export interface MarketClosureCreate {
  market: string;
  day: string;
  kind: MarketClosureKind;
  close_time?: string;
  note: string;
}

export interface VerifyChainResult {
  ok: boolean;
  checked: number;
  first_bad_journal_id: string | null;
}

export interface VerifyBalancesResult {
  ok: boolean;
  bad_accounts: string[];
}

export interface BackupFile {
  name: string;
  size_bytes: number;
  modified_at: string;
}

// --- broker ---------------------------------------------------------------------------

export type BrokerKind = "alpaca_paper" | "t212_demo" | "etoro_demo";

export interface BrokerLink {
  id: string;
  name: string;
  kind: BrokerKind | string;
  enabled: boolean;
  status: string; // ok | error | unknown
  last_error: string | null;
  /** fixed paper/demo endpoint (never editable) */
  base_url: string;
  /** secret values masked ("••••abcd") */
  config_masked: Record<string, string>;
  secret_keys: string[];
  portfolios: { id: string; name: string }[];
  created_at: string;
}

export interface BrokerLinkCreate {
  name: string;
  kind: BrokerKind;
  config: Record<string, string>;
}

/** omitted secret = kept, null = removed */
export interface BrokerLinkPatch {
  name?: string;
  enabled?: boolean;
  config?: Record<string, string | null>;
}

/** «Prova collegamento» (paper/demo links only): one checked step. */
export interface GuidedTestStep {
  key: "auth" | "account" | "buying_power" | "market" | "quote" | "order";
  label: string;
  status: "ok" | "error" | "skipped";
  detail: string;
}

export interface GuidedTestResult {
  ok: boolean;
  steps: GuidedTestStep[];
  checked_at: string;
}

export interface GuidedTestRequest {
  symbol: string;
  order_test: boolean;
  /** the second confirmation of the test order: GUIDED_ORDER_CONFIRM */
  confirm?: string;
}

export interface BrokerTestResult {
  ok: boolean;
  error: string | null;
  checked_at: string;
}

export interface ReconciliationDiff {
  symbol: string;
  ours: string;
  broker: string;
}

export interface Reconciliation {
  id: string;
  broker_link_id: string;
  portfolio_id: string;
  portfolio_name: string | null;
  ts: string;
  ok: boolean;
  diffs: ReconciliationDiff[];
}

// --- «Riconciliazione» (GET /api/reconciliation, plan 21) ------------------------------

export type CheckStatus = "ok" | "warning" | "mismatch" | "unverifiable";

/** One thing compared: a position, the cash, an open order. */
export interface CheckItem {
  key: string;
  kind: "position" | "cash" | "order";
  label: string;
  symbol: string | null;
  ours: string | null;
  broker: string | null;
  /** ours − broker */
  diff: string | null;
  status: CheckStatus;
  note: string;
  /** consecutive checks in mismatch (mismatch items only) */
  streak?: number;
  since?: string;
  /** the owner and the agents were alerted by this check */
  alerted?: boolean;
  portfolio_id?: string;
}

export interface ReconciliationCheck {
  id: number;
  ts: string;
  status: CheckStatus;
  trigger: "schedule" | "manual";
  error: string | null;
  meta: { currency?: string; orders_checked?: boolean; link_portfolios?: number; link_name?: string };
  counts: Record<CheckStatus, number>;
  /** the latest check only (not in the history) */
  items?: CheckItem[];
}

export interface ReconciliationPortfolio {
  portfolio_id: string;
  portfolio_name: string;
  linked: boolean;
  link: { id: string; name: string; kind: string; live: boolean; enabled: boolean } | null;
  latest: ReconciliationCheck | null;
  last_verified_at: string | null;
}

export interface ReconciliationDetail extends ReconciliationPortfolio {
  history: ReconciliationCheck[];
}

// --- Telegram notifications (GET /api/telegram, outbox, decisions) -------------------

/** skipped: paused for more than 24 h and not critical, never sent (old news) */
export type OutboxStatus = "pending" | "sent" | "failed" | "paused" | "skipped";

export interface TelegramSettings {
  configured: boolean;
  enabled: boolean;
  paused: boolean;
  /** "••••" when a token is stored; the token itself never leaves the server */
  bot_token_masked: string | null;
  bot_username: string | null;
  chat_id: string | null;
  chat_title: string | null;
  /** set when the stored token cannot be read: it must be entered again */
  error: string | null;
  outbox: { pending: number; paused: number; failed: number };
}

/** PUT /api/telegram/config: only the fields being changed. */
export interface TelegramConfigPatch {
  bot_token?: string;
  chat_id?: string;
  enabled?: boolean;
}

export interface TelegramChat {
  id: string | number;
  type: string;
  title: string | null;
}

export interface TelegramTestResult {
  ok: boolean;
  error?: string | null;
}

export interface OutboxItem {
  id: number;
  source: string;
  agent: string | null;
  category: string;
  level: string;
  text: string;
  status: OutboxStatus;
  attempts: number;
  last_error: string | null;
  created_at: string;
  sent_at: string | null;
  /** A document sent with the text as its caption (the weekly PDF report). */
  document_name?: string | null;
}

export type DecisionKind = "proposal" | "approval" | "veto" | "allocation" | "strategy" | "risk_rule" | "other" | "lesson" | "team_rule";

export type DecisionOutcome = "approvata" | "respinta" | "sospesa" | "modificata" | "informativa";

export interface Decision {
  id: number;
  agent: string | null;
  agent_id: string;
  portfolio: string | null;
  portfolio_id: string | null;
  kind: DecisionKind;
  summary: string;
  symbols: string[];
  details: Record<string, unknown>;
  outcome: DecisionOutcome | null;
  reason: string | null;
  subject: string | null;
  created_at: string;
}

export interface DecisionFilters {
  agent_id: string;
  portfolio_id: string;
  kind: string;
}

// --- «Istruzioni agenti» (/api/agent-docs) ---

export interface AgentDocSummary {
  slug: string;
  title: string;
  current_version: number;
  updated_at: string;
  /** admin email, or "seed" for the texts loaded from docs/paperclip-agents */
  updated_by: string;
}

export interface AgentDocDetail extends AgentDocSummary {
  body: string;
}

export interface AgentDocVersionInfo {
  version: number;
  author: string;
  note: string | null;
  created_at: string;
  /** bytes */
  size: number;
}

export interface AgentDocVersion {
  slug: string;
  version: number;
  body: string;
  author: string;
  note: string | null;
  created_at: string;
}

export interface AgentDocInput {
  body: string;
  note?: string | null;
  base_version: number;
}

export interface AgentDocExportStatus {
  last_export_at: string | null;
  ok: boolean | null;
  error: string | null;
  dir: string;
  entry_file: string;
  agents: { slug: string; path: string }[];
}

export interface AgentDocExportResult {
  exported: number;
  dir: string;
  ok: boolean;
  error?: string;
}

// --- real money («denaro reale») ------------------------------------------------------

export type LiveKind = "alpaca_live" | "t212_live" | "etoro_live";

/** The last read-only account snapshot (never the full account ID). */
export interface LiveSnapshot {
  currency: string | null;
  cash_available: string | null;
  positions_count: number | null;
  open_orders_count: number | null;
  crypto_enabled: boolean | null;
  order_types: string[] | null;
}

/** GET /api/live-links: never a key, only `key_masked` and `account_masked`. */
export interface LiveLink {
  id: string;
  name: string;
  kind: LiveKind | string;
  live_state: LiveState;
  armed_at: string | null;
  armed_by: string | null;
  disarmed_at: string | null;
  disarmed_reason: string | null;
  key_masked: string;
  account_masked: string | null;
  snapshot: LiveSnapshot | null;
  last_test_at: string | null;
  health_failures: number;
  /** the linked portfolios, sorted by name; `quota` in the account currency (null without a snapshot or FX rate) */
  portfolios: LiveLinkPortfolio[];
  /** the account currency (null before the first test) */
  currency: string | null;
  /** the broker's available cash, in `currency` */
  broker_cash: string | null;
  /** Σ quotas of the linked portfolios */
  quotas_total: string | null;
  /** broker_cash − quotas_total: negative when the quotas exceed the account's cash */
  unassigned: string | null;
}

export interface LiveLinkPortfolio {
  id: string;
  name: string;
  status: PortfolioStatus;
  base_currency: string;
  quota: string | null;
  positions_count: number;
}

/** POST /api/live-links (name + kind) and PUT /api/live-links/{id}/keys. */
export interface LiveKeysInput {
  name?: string;
  kind?: LiveKind;
  key_id?: string;
  secret_key?: string;
  api_key?: string;
  api_secret?: string;
  user_key?: string;
  crypto_enabled?: boolean;
}

/** POST /api/live-links/{id}/test: the snapshot read now; `empty` = no positions, no open orders. */
export interface LiveTestResult extends LiveSnapshot {
  account_masked: string | null;
  account_type: string | null;
  empty: boolean;
  account_changed: boolean;
}

export interface LiveArmInput {
  totp: string;
  confirm_name: string;
  accept_risks: boolean;
  dedicated_account: boolean;
  limits_set: boolean;
}

/** POST /api/live-links/{id}/disarm: `settled` false = disarmed, orders still to settle (retry). */
export interface LiveDisarmResult extends LiveLink {
  orders: unknown;
  settled: boolean;
}

export interface LiveStatus {
  armed_count: number;
  dry_run_count: number;
  portfolios: { id: string; name: string; live_state: LiveState }[];
  telegram_ok: boolean;
}

export interface LiveAuditRow {
  id: number;
  ts: string;
  link_id: string;
  link_name: string;
  portfolio_id: string | null;
  portfolio_name: string | null;
  order_id: string | null;
  actor: string;
  action: string;
  ip: string | null;
  user_agent: string | null;
  details: unknown;
}

export interface LiveAuditFilters {
  link_id: string;
  portfolio_id: string;
  action: string;
}

/** «Registro attività»: one state-changing request of a person, or an access event. */
/** `pending`: only the provisional row of a request whose final row is missing. */
export type ActivityOutcome = "ok" | "denied" | "error" | "pending";

export interface ActivityRow {
  id: number;
  ts: string;
  request_id: string;
  user_id: string | null;
  email: string | null;
  role: UserRole | null;
  action: string;
  action_label: string;
  category: string;
  category_label: string;
  method: string;
  route: string;
  target: Record<string, string>;
  outcome: ActivityOutcome;
  status: number | null;
  error_code: string | null;
  error_message: string | null;
  ip: string | null;
  user_agent: string | null;
  summary: Record<string, unknown>;
  provisional: boolean;
  /** provisional rows of the same request hidden behind this final row */
  provisional_rows: number;
}

export interface ActivityFilters {
  /** part of an email, any case */
  email: string;
  /** only rows without a user (failed logins, requests without a session) */
  anonymous: boolean;
  category: string;
  outcome: string;
  /** yyyy-mm-dd, inclusive (Europe/Rome days) */
  from: string;
  to: string;
}

export interface ActivityCatalog {
  categories: { key: string; label: string }[];
  actions: { key: string; label: string; category: string }[];
  outcomes: { key: ActivityOutcome; label: string }[];
}

// --- Paperclip (read-only reader: agent costs, isolation rules) ------------------------------

export interface PaperclipConfig {
  base_url: string | null;
  company_id: string | null;
  enabled: boolean;
  /** "••••abcd" when a board key is stored; the key itself never leaves the server */
  board_key_masked: string | null;
  /** Paperclip agent id → SMTrading agent id */
  agent_map: Record<string, string>;
  /** USD; used when the Paperclip company has no budget of its own (null = none) */
  monthly_budget_usd: string | null;
  /** set when the stored key cannot be read: it must be entered again */
  error: string | null;
}

/** PUT /api/paperclip/config: only the fields being changed; no `board_key` keeps the stored one. */
export interface PaperclipConfigPatch {
  base_url?: string;
  company_id?: string;
  board_key?: string;
  enabled?: boolean;
  agent_map?: Record<string, string>;
  /** null = no budget of our own */
  monthly_budget_usd?: string | null;
}

export interface PaperclipVerifyResult {
  ok: boolean;
  agents: { id: string; name: string | null; smt_agent: { id: string; name: string } | null }[];
  policies_count: number;
  error: string | null;
}

export type PaperclipIsolation = "ok" | "changed" | "unknown" | "off";

export interface PaperclipStatus {
  enabled: boolean;
  isolation: PaperclipIsolation;
  changes: { removed: string[]; changed: string[]; added: string[] };
  last_ok_at: string | null;
  last_error: string | null;
  consecutive_errors: number;
  baseline_at: string | null;
  /** "config": the company or the URL changed, the next read takes the new rules snapshot */
  rebaseline_reason: string | null;
  baseline_count: number;
  key_expires_at: string | null;
  /** the board key expires within 7 days (or has expired) */
  key_expiring: boolean;
  /** the agents' session refresh after an MCP tool catalog change */
  sessions?: PaperclipSessionsStatus;
  /** the last «PROTEGGI» issue the protection alarm could not create in Paperclip */
  protection_error?: string | null;
}

export interface PaperclipSessionsStatus {
  /** a catalog change whose refresh is not finished */
  pending: boolean;
  refreshed: number;
  /** agents left for the next pass (running, or the reset failed) */
  waiting: string[];
  /** agents whose reset failed 3 times */
  given_up: string[];
  last_error: string | null;
  updated_at: string | null;
}

export type AgentCostPeriod = "day" | "week" | "month";

export interface AgentCostRow {
  agent_id: string;
  name: string;
  paperclip_name: string | null;
  /** USD decimals as strings, as Paperclip reports them */
  api_usd: string;
  subscription_usd: string;
  other_usd: string;
  runs: number;
  /** the Paperclip model the agent is configured with now (null: not known) */
  model?: string | null;
  orders: number;
  portfolios: { id: string; name: string; permission: string; return_pct: string | null }[];
}

export interface AgentCosts {
  currency: "USD";
  from: string;
  to: string;
  last_read_at: string | null;
  agents: AgentCostRow[];
  unmatched: { paperclip_agent_id: string; name: string | null; api_usd: string; subscription_usd: string; other_usd: string }[];
  /** the current month (whatever the period): billed spend = API + other, never the subscription */
  month: AgentCostMonth;
  models?: AgentCostModels;
}

export interface AgentCostMonth {
  /** AAAA-MM */
  month: string;
  from: string;
  to: string;
  days_elapsed: number;
  days_in_month: number;
  spent_usd: string;
  daily_avg_usd: string;
  projected_usd: string;
  /** null when there is no budget (neither the Paperclip company's nor the settings' one) */
  budget_usd: string | null;
  budget_source: "paperclip" | "settings" | null;
  spent_pct: string | null;
  projected_pct: string | null;
  /** agent_id null = a Paperclip agent not matched to an SMTrading agent */
  shares: { agent_id: string | null; name: string; usd: string; pct: string | null }[];
}

/** GET /api/signals: how close each approved strategy is to an entry (decimals as strings). */
export type SignalDirection = "below" | "above" | "cross_up";
export type StrategySignalStatus = "ok" | "dati incompleti" | "non supportata";

export interface SignalSymbol {
  symbol: string;
  /** null (like the other fields below) when the symbol's data could not be read */
  last: string | null;
  indicator: string | null;
  /** on the last closed bar */
  value_closed: string | null;
  /** «se chiudesse ora» (provisional bar); null when the market is closed or the quote stale */
  value_now: string | null;
  threshold: string | null;
  direction: SignalDirection | null;
  distance: string | null;
  triggered_closed: boolean | null;
  triggered_now: boolean | null;
  as_of: string | null;
  /** the open position of the strategy's portfolio in this symbol (null: flat / no portfolio) */
  position?: { qty: string; avg_price: string } | null;
  /** a buy entry order of the symbol is still resting in that portfolio */
  open_entry?: boolean;
  /** in position: the strategy's exit rule on the newest data (null when flat) */
  exit?: SignalExit | null;
  /** the strategy's entry filters on the newest data (empty without filters) */
  filters?: SignalFilter[];
}

/** An entry filter: «Chiusura sopra SMA 200», «ATR 14 al massimo 3% del prezzo». A filter
 * that does not pass keeps the entry from firing even past its threshold. */
export interface SignalFilter {
  rule: string;
  value: string | null;
  threshold: string | null;
  passed: boolean;
}

/** `distance` > 0: still to go; <= 0 (with `triggered`): the exit fires. */
export interface SignalExit {
  /** "RSI 14 sopra 70", "SMA 20 sotto SMA 50", "Chiusura sotto minimo 10", "ROC 63 sotto 0",
   * "Chiusura sotto stop 2 × ATR 14" */
  rule: string;
  value: string;
  threshold: string;
  distance: string;
  triggered: boolean;
}

export interface StrategySignal {
  ref: string;
  name: string;
  type: string | null;
  params: Record<string, unknown>;
  timeframe: string | null;
  status: StrategySignalStatus;
  symbols: SignalSymbol[];
  /** intraday timeframe on a portfolio with mode A on (older servers: absent) */
  intraday?: boolean;
}

export interface SignalsResponse {
  as_of: string;
  /** portfolio_id/name null: the strategies without any portfolio (listed last) */
  portfolios: { portfolio_id: string | null; portfolio_name: string | null; strategies: StrategySignal[] }[];
}

// --- «Composizione» (GET /api/portfolios/{id}/composition) ----------------------------

/** One open position; last/value/weight/P&L are null when it has no quote at all. */
export interface CompositionPosition {
  symbol: string;
  name: string;
  asset_class: AssetClass;
  market: string;
  currency: string;
  qty: string;
  avg_price: string;
  last: string | null;
  value_eur: string | null;
  weight_pct: string | null;
  pnl_eur: string | null;
  pnl_pct: string | null;
  /** marked with the last cached quote */
  stale: boolean;
}

/** A group (asset class, «Contante» = key "cash", or a currency). */
export interface CompositionGroup {
  key: string;
  label: string;
  value_eur: string;
  weight_pct: string | null;
}

export interface Composition {
  as_of: string;
  /** null when a position has no quote (the total is unknown) */
  equity_eur: string | null;
  /** net cash (cash + collateral + margin loan) */
  cash_eur: string;
  positions: CompositionPosition[];
  by_asset_class: CompositionGroup[];
  by_currency: CompositionGroup[];
  unpriced_symbols: string[];
}

// --- «Posizioni» (GET /api/positions/open) ----------------------------------------------

export interface ProtectiveLeg {
  order_id: string;
  price: string | null;
  tif: "day" | "gtc";
}

export type Protection = "ok" | "senza stop" | "copre parziale" | "scade oggi";

export interface OpenPosition {
  portfolio_id: string;
  portfolio_name: string;
  symbol: string;
  name: string | null;
  market: string | null;
  currency: string;
  side: "long" | "short";
  qty: string;
  avg_price: string | null;
  opened_at: string | null;
  /** null (like value/pnl/distance/risk) when the quote cannot be read */
  last: string | null;
  /** marked with the last cached quote */
  stale: boolean;
  value_eur: string | null;
  pnl_eur: string | null;
  pnl_pct: string | null;
  stop: ProtectiveLeg | null;
  target: ProtectiveLeg | null;
  distance_to_stop_pct: string | null;
  risk_at_stop_eur: string | null;
  protection: Protection;
  /** US positions, regular session closed: the extended-hours price (information only) */
  extended?: ExtendedHours | null;
  /** the extended-hours price is at or beyond the stop: gap risk at the open */
  extended_beyond_stop?: boolean;
  /** being closed at the end of a «piano» (older servers: absent) */
  plan_state?: "pending" | "closing" | null;
  /** quantity bought today by day-trade orders and still open (null/absent: not in day mode) */
  day_qty?: string | null;
}

export interface OpenPositionsPortfolio {
  portfolio_id: string;
  portfolio_name: string;
  mode: PortfolioMode;
  live_state: LiveState | null;
  mode_label?: string | null;
  positions: number;
  value_eur: string | null;
  pnl_eur: string | null;
  risk_at_stop_eur: string;
  /** stops whose loss cannot be computed (no quote / FX) */
  risk_unknown: number;
  unprotected: number;
  /** positions whose stops cover less than their quantity */
  partial_cover: number;
  expiring_today: number;
  daily_loss_limit_eur: string | null;
  risk_pct_of_limit: string | null;
}

export interface OpenPositionsResponse {
  as_of: string;
  positions: OpenPosition[];
  portfolios: OpenPositionsPortfolio[];
}

/** GET /api/strategies/performance: one row of the «Strategie» page (EUR amounts as
 * strings; null = unknown, e.g. no quote or FX rate, shown «—»). */
export interface StrategyTrade {
  symbol: string;
  direction: "long" | "short";
  order_id: string;
  client_order_id: string | null;
  opened_at: string;
  closed_at: string | null;
  qty: string;
  open_qty: string;
  currency: string;
  entry_price: string;
  exit_price: string | null;
  mark: string | null;
  realized_pnl_eur: string | null;
  unrealized_pnl_eur: string | null;
  costs_eur: string | null;
  pnl_eur: string | null;
}

export interface StrategyPerformance {
  /** null: «Senza strategia» */
  ref: string | null;
  name: string | null;
  status: string | null;
  portfolio_id: string;
  portfolio_name: string;
  trades: number;
  closed_trades: number;
  open_positions: number;
  win_rate_pct: string | null;
  realized_pnl_eur: string | null;
  unrealized_pnl_eur: string | null;
  costs_eur: string | null;
  total_pnl_eur: string | null;
  first_trade_at: string;
  last_trade_at: string;
  trade_list: StrategyTrade[];
}

export interface StrategiesPerformanceResponse {
  as_of: string;
  strategies: StrategyPerformance[];
}

// --- «Risultati agenti» (GET /api/agent-results?period=…) ---

export type AgentResultsPeriod = "week" | "1m" | "3m" | "all";

export interface AgentResultsTeam {
  /** net realised P/L (EUR) of the trades closed in the period; null when a rate is missing */
  pnl_eur: string | null;
  costs_eur: string | null;
  orders_executed: number;
  proposals: number;
  approvals: number;
  vetoes: number;
  approval_rate_pct: string | null;
  lessons: number;
  lessons_repeated: number;
  rules_active: number;
  /** USD: API + subscription + other, every Paperclip agent */
  cost_usd: string;
  billed_usd: string;
  unmatched_cost_usd: string;
  pnl_per_usd: string | null;
  cost_per_order_usd: string | null;
}

export interface AgentResultRow {
  agent_id: string;
  name: string;
  active: boolean;
  decisions: Partial<Record<DecisionKind, number>>;
  decisions_total: number;
  proposals: number;
  approved: number;
  vetoed: number;
  pending: number;
  approval_rate_pct: string | null;
  lessons_written: number;
  lessons_about: number;
  lessons_repeated: number;
  mcp_calls: number;
  mcp_errors: number;
  mcp_error_pct: string | null;
  orders_executed: number;
  paperclip_names: string[];
  runs: number;
  cost_usd: string;
  billed_usd: string;
  cost_per_order_usd: string | null;
}

export interface StrategyResultRow {
  /** null: «Senza strategia» */
  ref: string | null;
  name: string | null;
  status: string | null;
  portfolios: string[];
  trades_opened: number;
  trades_closed: number;
  wins: number;
  win_rate_pct: string | null;
  realized_pnl_eur: string | null;
  costs_eur: string | null;
  net_pnl_eur: string | null;
  avg_holding_s: number | null;
  open_positions: number;
}

export interface AgentResults {
  as_of: string;
  period: AgentResultsPeriod;
  /** null for «tutto» */
  from: string | null;
  to: string;
  last_read_at: string | null;
  team: AgentResultsTeam;
  agents: AgentResultRow[];
  strategies: StrategyResultRow[];
}

// --- «Obiettivi» (GET/POST /api/goals, PATCH/DELETE /api/goals/{id}, GET /api/goals/progress) ---

export type GoalHorizon = "week" | "month" | "quarter" | "year" | "custom";
export type GoalScope = "total" | "portfolio";
/** "min": a gain to reach; "cap": a ceiling not to exceed; "max_loss": a loss not to
 * exceed (`target_pct` = the limit, positive); "beat_index": do better than `benchmark`
 * (`target_pct` = the margin in percentage points, "0" = just better) */
export type GoalKind = "min" | "cap" | "max_loss" | "beat_index";
export type GoalBenchmark = "SPY" | "QQQ" | "VWCE.DE";
export type GoalStatus =
  | "raggiunto"
  | "in linea"
  | "in ritardo"
  | "non iniziato"
  | "concluso"
  | "oltre il tetto"
  // max_loss
  | "nel limite"
  | "vicino al limite"
  | "superato"
  // beat_index (also "in linea")
  | "avanti"
  | "indietro";

/** A goal; `end_date` only for «custom»; `target_pct` a string ("2"). */
export interface Goal {
  id: string;
  name: string;
  horizon: GoalHorizon;
  start_date: string;
  end_date: string | null;
  recurring: boolean;
  scope: GoalScope;
  portfolio_id: string | null;
  target_pct: string;
  kind: GoalKind;
  /** beat_index only (absent from older servers) */
  benchmark?: GoalBenchmark | null;
  active: boolean;
  /** a «piano»: its positions are closed at the end of the period (older servers: absent) */
  closes_positions?: boolean;
  created_at: string;
  updated_at: string;
  portfolio_name: string | null;
  portfolio_archived: boolean;
}

export interface GoalCreate {
  name: string;
  horizon: GoalHorizon;
  start_date?: string;
  end_date?: string | null;
  recurring?: boolean;
  scope?: GoalScope;
  portfolio_id?: string | null;
  target_pct: string;
  kind?: GoalKind;
  /** required for beat_index, null otherwise */
  benchmark?: GoalBenchmark | null;
  active?: boolean;
  closes_positions?: boolean;
}

export type GoalPatch = Partial<GoalCreate>;

export type PlanClosureState = "pending" | "closing" | "closed";

/** `GET /api/goals/plans`: an active piano's latest ended period and its positions. */
export interface PlanState {
  goal_id: string;
  period_start: string;
  period_end: string;
  counts: Record<PlanClosureState, number>;
  positions: {
    portfolio_id: string;
    portfolio_name: string;
    symbol: string;
    state: PlanClosureState;
    closed_by: "system" | "agent" | null;
    closed_at: string | null;
  }[];
}

/** `GET /api/goals/templates`: a ready goal to confirm in the dialog. */
export interface GoalTemplate {
  key: string;
  label: string;
  body: GoalCreate;
  /** An equivalent active goal is already there (older servers: absent). */
  exists?: boolean;
}

/** The current period of an active goal (EUR strings; null without data). */
/** A portfolio's share of a goal's progress: they add up to current − start and to pct. */
export interface GoalPart {
  portfolio_id: string;
  name: string;
  change_eur: string;
  /** the points it adds to pct (null when pct is) */
  contribution_pct: string | null;
}

export interface GoalProgress {
  goal_id: string;
  name: string;
  horizon: GoalHorizon;
  scope: GoalScope;
  portfolio_id: string | null;
  portfolio_name: string | null;
  kind: GoalKind;
  period_start: string;
  period_end: string;
  start_eur: string | null;
  current_eur: string | null;
  target_eur: string | null;
  pct: string | null;
  target_pct: string;
  gap_eur: string | null;
  elapsed_pct: string;
  status: GoalStatus;
  as_of: string | null;
  /** beat_index: the index and its % in EUR over the same period; diff_pp = pct − benchmark_pct */
  benchmark?: GoalBenchmark | null;
  benchmark_pct?: string | null;
  diff_pp?: string | null;
  /** max_loss: max(0, −pct) */
  loss_pct?: string | null;
  /** each counted portfolio's share, by name (absent from older servers) */
  parts?: GoalPart[];
}

// --- campaigns («Campagne», plan 21) ------------------------------------------------

export type CampaignStatus = "bozza" | "attiva" | "conclusa";

export interface CampaignPortfolio {
  id: string;
  name: string;
  status: PortfolioStatus;
  base_currency: string;
  /** created by the campaign wizard (false: an existing portfolio) */
  created: boolean;
  broker_link_name: string | null;
  broker_kind: string | null;
}

/** `GET /api/campaigns`: a named test run grouping portfolios over a period. */
export interface Campaign {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  notes: string | null;
  status: CampaignStatus;
  created_by: string | null;
  created_at: string;
  closed_at: string | null;
  closed_by: string | null;
  portfolios: CampaignPortfolio[];
  goals: Goal[];
}

/** A portfolio of the wizard: an existing one (`portfolio_id`) or a new one. */
export interface CampaignPortfolioSpec {
  key: string;
  portfolio_id?: string;
  name?: string;
  initial_cash?: string;
  base_currency?: string;
  risk_profile_id?: string | null;
  /** a paper/demo link only (real-money links are refused) */
  broker_link_id?: string | null;
}

export interface CampaignGoalSpec extends GoalCreate {
  portfolio_key: string;
}

export interface CampaignCreate {
  name: string;
  start_date: string;
  end_date: string;
  notes?: string | null;
  status?: "bozza" | "attiva";
  portfolios: CampaignPortfolioSpec[];
  goals: CampaignGoalSpec[];
}

/** `POST /api/campaigns/templates`: a ready goal of the wizard's third step. */
export interface CampaignTemplate {
  key: string;
  label: string;
  portfolio_key: string;
  /** pre-selected in the wizard (the campaign's own minimum and maximum loss) */
  recommended: boolean;
  body: GoalCreate;
}

export interface CampaignCloseCheck {
  campaign_id: string;
  status: CampaignStatus;
  open_positions: number;
  open_orders: number;
  portfolios: {
    id: string;
    name: string;
    status: PortfolioStatus;
    archived: boolean;
    broker_link_name: string | null;
    positions: { symbol: string; qty: string }[];
    open_orders: { id: string; symbol: string; side: string; type: string; status: string; qty: string | null }[];
    can_archive: boolean;
    /** why it cannot be archived now (the archive rules' message) */
    archive_blocker: string | null;
  }[];
  plans: (PlanState & { goal_name: string | null })[];
}

// --- «Diario operazioni» (GET /api/trades, /api/trades/{key}) ---

export type TradeOutcome = "win" | "loss" | "open";
export type TradePeriod = "week" | "1m" | "3m" | "all";

export interface TradeRow {
  key: string;
  portfolio_id: string;
  portfolio_name: string;
  symbol: string;
  currency: string;
  direction: "long" | "short";
  /** null: «Senza strategia» */
  strategy_ref: string | null;
  strategy_name: string | null;
  proposal_ref: string | null;
  order_id: string;
  opened_at: string;
  closed_at: string | null;
  outcome: TradeOutcome;
  qty: string;
  open_qty: string;
  entry_price: string;
  exit_price: string | null;
  mark: string | null;
  realized_pnl_eur: string | null;
  unrealized_pnl_eur: string | null;
  costs_eur: string | null;
  pnl_eur: string | null;
  pnl_pct: string | null;
  holding_s: number;
  exit_reason: string | null;
  exit_reason_label: string | null;
  exit_reasons: string[];
}

export interface TradesResponse {
  as_of: string;
  period: TradePeriod;
  total: number;
  page: number;
  page_size: number;
  summary: { trades: number; open: number; closed: number; wins: number; win_rate_pct: string | null; pnl_eur: string | null };
  filters: {
    portfolios: { id: string; name: string }[];
    strategies: { ref: string; name: string | null }[];
    no_strategy: boolean;
    symbols: string[];
  };
  items: TradeRow[];
}

export interface TradeFilters {
  period: TradePeriod;
  portfolio_id: string;
  strategy: string;
  symbol: string;
  outcome: "" | TradeOutcome;
  page: number;
}

export interface TradeDecision {
  id: number;
  kind: string;
  created_at: string;
  agent: string | null;
  summary: string;
  subject: string | null;
  outcome: string | null;
  reason: string | null;
  symbols: string[];
  details: Record<string, unknown>;
}

export interface TradeOrder {
  id: string;
  label: string;
  role: string | null;
  client_order_id: string | null;
  side: "buy" | "sell";
  type: string;
  tif: string;
  qty: string | null;
  filled_qty: string;
  limit_price: string | null;
  stop_price: string | null;
  avg_fill_price: string | null;
  status: string;
  source: string;
  agent: string | null;
  reduce_only: boolean;
  parent_id: string | null;
  oco_group_id: string | null;
  created_at: string;
  updated_at: string;
  reject_code: string | null;
  reject_message: string | null;
  cancel_reason: string | null;
}

export interface TradeFx {
  sold: { currency: string; amount: string } | null;
  bought: { currency: string; amount: string } | null;
  rate: string | null;
  fee: Record<string, string>;
}

export interface TradeFill {
  fill_id: string;
  order_id: string;
  kind: "entry" | "exit";
  side: "buy" | "sell";
  ts: string;
  qty: string;
  fill_qty: string;
  price: string;
  currency: string;
  fee_amount: string | null;
  fee_currency: string | null;
  fx: TradeFx | null;
  exit_reason: string | null;
  exit_reason_label: string | null;
}

export interface TradeChange {
  id: number;
  ts: string;
  method: string;
  outcome: string;
  agent: string | null;
  order_id: string | null;
  changes: Record<string, unknown>;
}

export interface TradeTimelineItem {
  ts: string;
  type: string;
  title: string;
  text: string;
  decision_id: number | null;
  order_id: string | null;
}

export interface TradeDetail {
  as_of: string;
  trade: TradeRow;
  instrument_name: string | null;
  proposal: TradeDecision | null;
  decisions: TradeDecision[];
  lessons: TradeDecision[];
  orders: TradeOrder[];
  fills: TradeFill[];
  changes: TradeChange[];
  activity: { id: number; ts: string; type: string; level: string; summary: string; data: Record<string, unknown> }[];
  timeline: TradeTimelineItem[];
}

// --- «Strategie: previsto contro reale» (GET /api/strategies/expectations) ---

export type ExpectationVerdict = "in linea" | "sotto le attese" | "dati insufficienti";

export interface StrategyExpectation {
  ref: string;
  name: string;
  status: string;
  approved_at: string;
  expected: {
    source: string | null;
    backtest_id: string | null;
    from_date: string | null;
    to_date: string | null;
    trades: number | null;
    trades_per_month: string | null;
    win_rate_pct: string | null;
    avg_trade_pct: string | null;
    max_drawdown_pct: string | null;
    sharpe: string | null;
    total_return_pct: string | null;
  };
  live: {
    since: string;
    trades: number;
    open_trades: number;
    closed_trades: number;
    wins: number;
    win_rate_pct: string | null;
    avg_trade_pct: string | null;
    pnl_eur: string | null;
    max_drawdown_pct: string | null;
    max_drawdown_eur: string | null;
    trades_per_month: string | null;
    days: number;
  };
  verdict: ExpectationVerdict;
  reasons: string[];
}

export interface StrategyExpectationsResponse {
  as_of: string;
  min_trades: number;
  strategies: StrategyExpectation[];
}

/** GET /api/system-health («Salute del sistema»), cached 30 s by the api. */
export type HealthStatus = "ok" | "attenzione" | "problema" | "spento";

export interface HealthSectionBase {
  status: HealthStatus;
  label: string;
  summary: string;
  /** what is wrong, in plain Italian (empty when ok) */
  reasons: string[];
}

export interface HealthJob {
  name: string;
  status: HealthStatus;
  note: string | null;
  started_at: string | null;
  last_tick_at: string | null;
  interval_s: number | null;
  crashes: number;
  last_crash_at: string | null;
  last_error: string | null;
  /** small facts the job reports with each pass (the scalper: `active_rules`) */
  info?: Record<string, unknown> | null;
}

export interface HealthProvider {
  name: string;
  status: HealthStatus;
  health: string;
  last_error: string | null;
  last_checked_at: string | null;
}

export interface HealthQuoteClass {
  asset_class: string;
  symbols: number;
  fresh: number;
  stale: number;
  /** market closed: an old quote is expected */
  closed: number;
  threshold_s: number;
  stale_symbols: string[];
}

export interface HealthCycleRoutine {
  routine: string | null;
  agent: string | null;
  expected: number;
  day_total: number;
  done: number;
  completed: number;
}

export interface HealthCycles {
  day: string;
  read_at: string | null;
  error: string | null;
  expected: number;
  day_total: number;
  done: number;
  missing: number;
  routines: HealthCycleRoutine[];
  invalid_cron: string[];
}

export interface HealthPriceQuality {
  suspects_1h: number;
  suspects_24h: number;
  top_symbols: { symbol: string; count: number }[];
  by_reason: { reason: string; label: string; count: number }[];
  by_provider: { provider: string; counts: Record<string, number> }[];
  /** symbols whose quotes are unconfirmed right now */
  current: { symbol: string; since: string | null; age_s: number }[];
}

/** One problem the «Sentinella di sistema» told the owner about and that is still open. */
export interface HealthSentinelProblem {
  key: string;
  kind: string;
  /** the kind in Italian («Agenti Paperclip», «Errori MCP ripetuti» …) */
  label: string;
  title: string;
  detail: string;
  level: "info" | "warning" | "critical";
  since: string | null;
}

/** «Sentinella di sistema»: outside the overall status (it alerts on that status). */
export interface HealthSentinel extends HealthSectionBase {
  on: boolean | null;
  last_pass_at: string | null;
  last_error: string | null;
  open: HealthSentinelProblem[];
  alerts_7d: number;
}

export interface SystemHealth {
  status: HealthStatus;
  reasons: string[];
  checked_at: string;
  /** Missing on an api older than the sentinel. */
  sentinel?: HealthSentinel;
  version: { build: string; built_at: string | null; api_started_at: string | null; worker_started_at: string | null };
  sections: {
    services: HealthSectionBase & { heartbeat_age_s?: number | null; worker_started_at?: string | null; jobs?: HealthJob[] };
    providers: HealthSectionBase & { providers?: HealthProvider[] };
    quotes: HealthSectionBase & { classes?: HealthQuoteClass[] };
    paperclip: HealthSectionBase & {
      reader?: { enabled: boolean; last_ok_at?: string | null; last_error?: string | null; consecutive_errors?: number; isolation?: string };
    };
    cycles: HealthSectionBase & { cycles?: HealthCycles };
    watchdog: HealthSectionBase & { warnings?: { at: string | null; text: string }[] };
    telegram: HealthSectionBase & {
      pending?: number;
      failed_24h?: number;
      paused?: boolean;
      configured?: boolean;
      oldest_pending_at?: string | null;
      oldest_pending_age_s?: number | null;
    };
    costs: HealthSectionBase & { today_usd?: string; month?: AgentCostMonth };
    mcp: HealthSectionBase & { calls?: number; errors?: number; internal?: number; by_code?: { code: string; count: number }[] };
    /** Missing on an api older than the automatic backups. */
    backup?: HealthSectionBase & {
      last_ok_at?: string | null;
      last_ok_name?: string | null;
      last_ok_age_s?: number | null;
      last_backup?: HealthBackupRun | null;
      last_restore_test?: HealthBackupRun | null;
      restore_age_s?: number | null;
      destinations?: { dir: string; files: number; error: string | null }[];
    };
    price_quality?: HealthSectionBase & Partial<HealthPriceQuality>;
    quotas?: HealthSectionBase & { providers?: { name: string; quota: ProviderQuota }[]; note?: string };
  };
}

/** One automatic backup run or restore test (`status`: "ok" | "parziale" | "non riuscito"). */
export interface HealthBackupRun {
  kind: "backup" | "restore_test";
  status: string;
  trigger: string;
  started_at: string;
  finished_at?: string;
  duration_s?: number;
  name: string | null;
  size_bytes: number | null;
  error: string | null;
}

/** «Costi agenti» → «Per modello» (decimals as strings, USD). */
export interface ModelCostRow {
  model: string;
  billed_usd: string;
  api_usd: string;
  subscription_usd: string;
  other_usd: string;
  runs: number;
  /** runs without a model in Paperclip: the agent's configured model at the time */
  derived_runs: number;
  billed_per_run_usd: string | null;
  /** with the value of the runs included in the subscription */
  full_per_run_usd: string | null;
  agents: string[];
}

export interface ModelChange {
  agent_id: string;
  name: string;
  from: string;
  to: string;
  at: string;
}

export interface AgentCostModels {
  rows: ModelCostRow[];
  agents: { paperclip_agent_id: string; name: string; model: string | null; since: string | null }[];
  /** newest first */
  changes: ModelChange[];
  updated_at: string | null;
}

export interface ModelCompare {
  at: string;
  days: number;
  before: { from: string; to: string; models: ModelCostRow[] };
  after: { from: string; to: string; models: ModelCostRow[] };
  /** a model's day of `at` with runs on both sides: left out */
  straddling: { agent: string; model: string; runs: number }[];
}

/** GET /api/strategies/research: the agents' research runs (walk-forward, sweep, portfolio). */
export type ResearchKind = "walk_forward" | "param_sweep" | "portfolio";
export type ResearchStatus = "queued" | "running" | "completed" | "failed";
export type WalkForwardVerdict = "solida" | "fragile" | "da scartare" | "dati insufficienti";
export type ParamValue = number | string;

export interface ResearchMetrics {
  total_return_pct?: string;
  cagr_pct?: string;
  max_drawdown_pct?: string;
  sharpe?: string;
  win_rate_pct?: string;
  trades?: number;
  exposure_pct?: string;
  positive_windows_pct?: string;
  efficiency?: string | null;
  [key: string]: string | number | null | undefined;
}

export interface ResearchSummary {
  verdict?: WalkForwardVerdict;
  oos_metrics?: ResearchMetrics;
  stability_pct?: string;
  best_by_metric?: Record<string, ParamValue>;
  best_by_robust?: Record<string, ParamValue>;
  isolated_peak?: boolean;
  metrics?: ResearchMetrics;
}

export interface ResearchRun {
  id: string;
  kind: ResearchKind;
  agent: string | null;
  strategy: string | null;
  symbols: string[];
  timeframe: string;
  from_date: string;
  to_date: string;
  initial_cash: string;
  spec: { sleeves?: { strategy: string; symbols: string[] }[]; [key: string]: unknown };
  portfolio_id: string | null;
  status: ResearchStatus;
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  summary?: ResearchSummary | null;
  result?: ResearchResult | null;
}

export interface ResearchEquityPoint {
  ts: string;
  equity: string;
  drawdown_pct?: string;
}

export interface ResearchResult {
  // walk-forward
  windows?: {
    window: number;
    oos_from: string;
    oos_to: string;
    best_params: Record<string, ParamValue>;
    is_metrics: ResearchMetrics;
    oos_metrics: ResearchMetrics;
  }[];
  oos_metrics?: ResearchMetrics;
  verdict?: WalkForwardVerdict;
  reasons?: string[];
  oos_equity?: ResearchEquityPoint[];
  // sweep
  rows?: { rank: number; params: Record<string, ParamValue>; metrics: ResearchMetrics; robust: string; neighbour_median: string | null }[];
  metric?: string;
  heatmap?: { x_param: string; y_param: string; x_values: ParamValue[]; y_values: ParamValue[]; metric: string; z: (string | null)[][] } | null;
  // portfolio
  metrics?: ResearchMetrics;
  equity?: ResearchEquityPoint[];
  contribution?: { sleeve: number; strategy: string; symbols: string[]; pnl: string; contribution_pct: string; trades: number; win_rate_pct: string }[];
  correlation?: { labels: string[]; matrix: (string | null)[][] };
}

export interface ResearchRunsResponse {
  runs: ResearchRun[];
}

// --- Intraday (GET /api/intraday/triggers, /api/intraday/accounts) ----------------------

export type IntradayOutcome = "pending" | "woken" | "telegram" | "capped" | "issue_open" | "no_agent" | "late" | "failed";

/** One signal that fired on an intraday timeframe (mode A), newest first. */
export interface IntradayTrigger {
  id: number;
  created_at: string;
  portfolio_id: string;
  portfolio_name: string;
  strategy_ref: string;
  strategy_name: string;
  symbol: string;
  rule: "entry" | "exit";
  timeframe: string;
  bar_close: string;
  value: string | null;
  threshold: string | null;
  price: string | null;
  rule_text: string;
  outcome: IntradayOutcome;
  issue_ident: string | null;
  error: string | null;
}

/** A portfolio of a broker link, with its day-trade switches and cap usage. */
export interface IntradayAccountPortfolio {
  portfolio_id: string;
  portfolio_name: string;
  intraday_enabled: boolean;
  auto_rules_enabled: boolean;
  leverage: string;
  /** the leverage a day-trade entry may use */
  day_leverage: string;
  max_day_trades_5d: number | null;
  /** the cap usage: null when the portfolio is not in day mode */
  used: number | null;
  pending: number | null;
  remaining: number | null;
  /** yyyy-mm-dd of the next session when a slot frees up */
  frees_on: string | null;
}

/** The broker account state last read for a link (nothing here identifies the account). */
export interface IntradayAccount {
  link_id: string;
  link_name: string;
  kind: string;
  buying_power: string | null;
  multiplier: string | null;
  equity: string | null;
  trading_blocked: boolean | null;
  account_blocked: boolean | null;
  read_at: string | null;
  /** read recently enough to be trusted */
  fresh: boolean;
  /** multiplier above 1: leverage is available (every Alpaca account is a margin account) */
  margin: boolean;
  portfolios: IntradayAccountPortfolio[];
}

// --- Automatic rules (mode B): /api/auto-rules ------------------------------------------

export type AutoRuleStatus = "draft" | "active" | "paused" | "braked";
export type AutoRuleStream = "stream" | "polling" | "none";
export type AutoParamKind = "dec" | "int" | "choice" | "opt_dec";
/** decimals arrive and travel as numeric strings, integers as numbers */
export type AutoValue = string | number | null;

export interface ModelParam {
  name: string;
  kind: AutoParamKind;
  lo: AutoValue;
  hi: AutoValue;
  default: AutoValue;
  choices: string[];
  /** a parameter every model has (stop, target, times, spread) */
  common: boolean;
}

export interface ModelSpec {
  name: string;
  params: ModelParam[];
}

export interface AutoRule {
  id: string;
  portfolio_id: string;
  portfolio_name: string;
  symbol: string;
  model: string;
  status: AutoRuleStatus;
  status_it: string;
  status_reason: string | null;
  status_changed_at: string | null;
  version: number;
  params: Record<string, AutoValue>;
  qty: string | null;
  notional: string | null;
  brakes: Record<string, AutoValue>;
  stream: AutoRuleStream;
  stream_it: string;
  open_trade: boolean;
  /** the figures of the portfolio's local day (the brakes count the same day) */
  today: { trades: number; net: string; net_currency: string };
}

export interface AutoRuleBody {
  params?: Record<string, AutoValue>;
  qty?: string;
  notional?: string;
  brakes?: Record<string, AutoValue>;
}

export interface AutoRuleCreate extends AutoRuleBody {
  portfolio_id: string;
  symbol: string;
  model: string;
}

export type AutoExitReason = "stop" | "target" | "time" | "session" | "manual" | "day_close";

export interface AutoTrade {
  id: number;
  rule_id: string;
  rule_version: number;
  symbol: string;
  trigger_at: string;
  trigger_price: string;
  expected_price: string;
  status: "sent" | "open" | "closed" | "canceled" | "rejected";
  filled_at: string | null;
  fill_price: string | null;
  qty: string | null;
  exit_reason: AutoExitReason | null;
  exit_filled_at: string | null;
  exit_expected: string | null;
  exit_price: string | null;
  fees: string;
  fees_currency: string | null;
  net_result: string | null;
  net_currency: string | null;
  entry_slippage_bps: string | null;
  exit_slippage_bps: string | null;
  ms_trigger_to_send: number | null;
  ms_send_to_broker: number | null;
  ms_broker_to_fill: number | null;
  error: string | null;
}

/** One rule on one UTC day (GET /api/auto-rules/results). */
export interface AutoRuleResult {
  rule_id: string;
  version: number;
  /** YYYY-MM-DD, UTC */
  day: string;
  trades: number;
  wins: number;
  losses: number;
  net: string;
  net_currency: string | null;
  /** per currency */
  fees: Record<string, string>;
  median_ms_trigger_to_send: string | null;
  median_ms_send_to_broker: string | null;
  median_ms_broker_to_fill: string | null;
  avg_entry_slippage_bps: string | null;
  /** blocked entries by reason */
  blocked: Record<string, number>;
}

export interface AutoRuleResultsParams {
  portfolio_id?: string;
  from?: string;
  to?: string;
}

export interface ReplayTrade {
  symbol: string;
  entry_ts: string;
  entry_price: string;
  exit_ts: string;
  exit_price: string;
  exit_reason: "target" | "stop" | "time" | "session" | "end";
  qty: string;
  fees: string;
  net: string;
}

export interface ReplayResult {
  symbols: string[];
  from: string;
  to: string;
  trades: ReplayTrade[];
  trades_total: number;
  truncated: boolean;
  net: string;
  fees: string;
  wins: number;
  losses: number;
  max_drawdown: string;
  /** the limits of the minute replay, to show as they are */
  notes: string[];
  per_symbol: { symbol: string; trades: number; net: string; fees: string; wins: number; losses: number; max_drawdown: string }[];
}

export interface AutoBacktestBody extends AutoRuleBody {
  symbol: string;
  model: string;
  from: string;
  to: string;
  spread_pct: string;
  delay_s: number;
  fee_rule: "broker" | "zero";
  portfolio_id?: string;
}

// «Fiume dei flussi» (GET /api/flow/map | events | summary; backend smtrading.flow)

/** a node of the flow map: `kind` is the colour family, `column` 0..5 left to right */
export interface FlowNode {
  key: string;
  label: string;
  kind: string;
  column: number;
  description: string;
}

/** nodes and links (each link [a, b] in its forward direction, key "a|b") */
export interface FlowMap {
  nodes: FlowNode[];
  links: [string, string][];
}

export interface FlowEvent {
  id: string;
  ts: string;
  kind: string;
  who: string;
  path: string[];
  hops: string[];
  outcome: "ok" | "error";
  latency_ms: number | null;
  symbol: string | null;
}

export interface FlowEventsResponse {
  events: FlowEvent[];
  cursor: string;
  now: string;
}

export interface FlowLinkSummary {
  calls: number;
  errors: number;
  top_tools: { tool: string; calls: number }[];
}

export type FlowWindow = 1 | 5 | 15;

export interface FlowSummary {
  /** keyed "a|b" in the link's forward direction */
  links: Record<string, FlowLinkSummary>;
  /** events touching the node, per minute */
  nodes: Record<string, number>;
  head: {
    calls_per_min: number;
    orders_today: number;
    errors_window: number;
    mcp_latency_ms: number | null;
  };
}
