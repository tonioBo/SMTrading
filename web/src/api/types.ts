/** Admin API payloads. Decimals are strings on the wire. */

export interface AdminUser {
  email: string;
  totp_enabled: boolean;
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

/** simulazione = no broker (virtual money); paper = demo broker account; reale reserved */
export type PortfolioMode = "simulazione" | "paper" | "reale";

export interface DashboardPortfolio {
  id: string;
  name: string;
  status: string;
  mode: PortfolioMode;
  equity_eur: string | null;
  day_pnl_eur: string | null;
  day_pnl_pct: string | null;
  stale_symbols: string[];
  error: string | null;
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

export interface EquitySeries {
  period: EquityPeriod;
  bucket_s: number;
  series: EquityPoint[];
  portfolios: { id: string; name: string | null; series: EquityPoint[] }[];
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
  /** live portfolios using it (list only; Default also counts those without a profile) */
  portfolios_count?: number;
}

export type AgentPermission = "read" | "trade";

export interface AgentPortfolioLink {
  portfolio_id: string;
  portfolio_name: string;
  permission: AgentPermission;
  archived?: boolean;
  mode?: PortfolioMode;
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
}

export type InterestKind = "loan" | "borrow";

export interface InterestRate {
  kind: InterestKind;
  key: string;
  annual_pct: string;
}

export type AssetClass = "us_equity" | "eu_equity" | "crypto" | "forex";

export interface AppSettings {
  stale_quote_s: Record<string, number>;
  default_spread_bps: Record<string, number>;
  timezone: string;
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

export interface Quote {
  symbol: string;
  bid: string | null;
  ask: string | null;
  last: string | null;
  ts: string | null;
  provider: string | null;
}

/** GET /api/quotes: a quote per symbol, or `{error}` for a symbol that failed. */
export type QuotesResponse = Record<string, Quote | { error: string }>;

export interface MarketStatus {
  market: string;
  is_open: boolean;
  next_open: string | null;
  next_close: string | null;
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

export type BrokerKind = "alpaca_paper" | "t212_demo";

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

export type DecisionKind = "proposal" | "approval" | "veto" | "allocation" | "strategy" | "risk_rule" | "other";

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
  created_at: string;
}

export interface DecisionFilters {
  agent_id: string;
  portfolio_id: string;
  kind: string;
}
