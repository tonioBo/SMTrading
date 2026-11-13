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

export interface KillSwitchResult extends KillSwitchState {
  canceled: number;
}

export interface DashboardPortfolio {
  id: string;
  name: string;
  status: string;
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
  connectors: DashboardConnector[];
  margin_alerts: MarginAlert[];
  kill_switch: boolean;
  worker_heartbeat_age_s: number | null;
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

export type PortfolioStatus = "active" | "reduce_only" | "locked";

export interface Portfolio {
  id: string;
  name: string;
  base_currency: string;
  leverage: string;
  maintenance_margin_pct: string;
  warning_margin_pct: string;
  slippage_bps: number;
  status: PortfolioStatus;
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
}
