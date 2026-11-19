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
  total_equity_eur: string;
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
  /** only in a save response: set when the name has a word the agents should not see */
  name_warning?: string | null;
}

export interface PortfolioCreate {
  name: string;
  initial_cash: string;
  base_currency: string;
  leverage: string;
  slippage_bps: number;
  timezone: string;
  risk_profile_id: string | null;
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
}

/** POST /api/portfolios/{id}/orders/preview: a refused check is ok=false with its code. */
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

export interface AppSettings {
  stale_quote_s: Record<string, number>;
  default_spread_bps: Record<string, number>;
  timezone: string;
  mcp_hide_mode: boolean;
  weekly_goal?: WeeklyGoal;
  preopen_check?: PreopenCheck;
  handoff_watchdog?: HandoffWatchdog;
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
}

/** PATCH merge: omitted secret = kept, null = removed, unchanged masked value = kept. */
export interface ConnectorPatch {
  enabled?: boolean;
  config?: Record<string, unknown>;
  rate_limit_per_min?: number;
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
  streams: Record<string, { symbols: string[]; state: StreamState | string }>;
  polling: string[];
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
  plan_state?: "pending" | "grace" | "closing" | null;
  /** its grace deadline, while in grace */
  plan_grace_until?: string | null;
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

export type PlanClosureState = "pending" | "grace" | "closing" | "closed";

/** `GET /api/goals/plans`: an active piano's latest ended period and its positions. */
export interface PlanState {
  goal_id: string;
  period_start: string;
  period_end: string;
  grace_days: number;
  counts: Record<PlanClosureState, number>;
  positions: {
    portfolio_id: string;
    portfolio_name: string;
    symbol: string;
    state: PlanClosureState;
    deadline: string | null;
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
