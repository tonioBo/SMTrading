// «Schermo TV» (backend smtrading.tv: GET/POST /api/tv…, GET /api/tv-display/…).
// Timestamps are ISO strings, decimals strings.

import type { FlowEvent, FlowHealth, FlowMap, FlowSummary } from "@/api/types";

export type TvScreenKey = "flow" | "campaign" | "health";
export type TvPairingState = "idle" | "waiting" | "ok" | "refused" | "timeout" | "error";

export interface Tv {
  id: string;
  name: string;
  host: string;
  model: string | null;
  paired: boolean;
  paired_at: string | null;
  pairing: { state: TvPairingState; message: string | null; started_at: string | null };
  last_used_at: string | null;
  last_ok: boolean | null;
  last_message: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface TvSound {
  enabled: boolean;
  volume: number;
  quiet_start: string | null;
  quiet_end: string | null;
  tz: string;
  quiet_now: boolean;
}

/** GET /api/tv: the user's own area */
export interface TvArea {
  screens: { key: TvScreenKey; label: string }[];
  tvs: Tv[];
  display: {
    screen: TvScreenKey;
    rotation: boolean;
    interval_s: number;
    version: number;
    updated_at: string;
    portal_url: string | null;
  };
  sound: TvSound;
  link: { active: boolean; created_at: string | null; url: string | null };
  portal_url_detected: string | null;
  can_discover: boolean;
  suggested_subnet: string | null;
  interval: { min: number; max: number };
  network_note: string;
}

export type TvPower = "on" | "standby" | "unreachable" | "not_samsung";

export interface TvSendResult {
  ok: boolean;
  message: string;
  url: string;
  powered_on: boolean;
  app_id: string | null;
  confirmed: boolean;
}

export interface TvDisplayPatch {
  screen?: TvScreenKey;
  rotation?: boolean;
  interval_s?: number;
  portal_url?: string;
  sound_enabled?: boolean;
  sound_volume?: number;
  quiet_start?: string | null;
  quiet_end?: string | null;
}

export interface TvDiscovered {
  host: string;
  name: string;
  model: string | null;
  resolution: string | null;
  mac: string | null;
  power: "on" | "standby";
}

export interface TvAdminItem {
  id: string;
  user_id: string;
  user_email: string;
  name: string;
  host: string;
  model: string | null;
  paired: boolean;
  paired_at: string | null;
  last_used_at: string | null;
  last_ok: boolean | null;
  revoked_at: string | null;
  revoked_by: string | null;
  created_at: string;
}

// ---- the TV pages (display link)

export type TvSoundKind = "fill_buy" | "fill_sell" | "stop" | "target" | "health_bad" | "health_ok";

export interface TvSoundEvent {
  id: string;
  kind: TvSoundKind;
  side?: "buy" | "sell";
  symbol?: string;
  label?: string;
  at: string;
}

export interface TvSelection {
  screen: TvScreenKey;
  rotation: boolean;
  interval_s: number;
  version: number;
  updated_at: string;
  screens: TvScreenKey[];
  poll_s: number;
  now: string;
  sound: TvSound;
  sound_events: TvSoundEvent[];
  sound_cursor: string;
}

export interface TvFlowData {
  screen: "flow";
  now: string;
  map: FlowMap;
  summary: FlowSummary;
  health: FlowHealth;
  events: FlowEvent[];
  cursor: string;
}

export interface TvCampaignData {
  screen: "campaign";
  now: string;
  campaign: { name: string; start: string; end: string; day: number; total_days: number; state: "prima" | "in_corso" | "conclusa" } | null;
  equity: { current_eur: string; as_of: string; portfolios: number } | null;
  goal: {
    label: string;
    name: string;
    status: string;
    start_eur: string;
    current_eur: string;
    target_eur: string;
    pct: string;
    target_pct: string;
    period_start: string;
    period_end: string;
    as_of: string;
  } | null;
  portfolios: { id: string; name: string; code: string; equity_eur: string | null; day_change_eur: string | null; day_change_pct: string | null; as_of: string | null }[];
  positions: {
    portfolio: string;
    symbol: string;
    qty: string;
    avg_price: string;
    opened_at: string;
    stop_price: string | null;
    stop_type: "stop" | "stop_limit" | "trailing_stop" | null;
    trail_pct: string | null;
  }[];
  fills: { portfolio: string; symbol: string; side: "buy" | "sell"; qty: string; price: string; at: string }[];
  notes: string[];
}

export type TvStatus = "ok" | "attenzione" | "problema";

export interface TvHealthData {
  screen: "health";
  now: string;
  status: TvStatus;
  checked_at: string;
  sections: { key: string; label: string; status: TvStatus | "spento"; summary: string }[];
  nodes: { key: string; label: string; kind: string; state: "ok" | "attenzione" | "errore" | "sconosciuto"; reason: string }[];
  alerts: { on: boolean; open: { title: string; label: string; level: string; since: string }[]; recent: { label: string; at: string }[] };
}
