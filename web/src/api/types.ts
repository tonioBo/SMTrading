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
