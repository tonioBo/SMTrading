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
