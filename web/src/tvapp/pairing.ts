/**
 * The app's connection to SMTrading, as a state machine (the effects live in TvApp):
 *
 *   setup ──configured──▶ code (no token) | connecting (a stored token)
 *   code: ask a code (POST /code) → show it → poll it; `claimed` brings the token in the
 *         body (the http entry of the app, header X-SMT-TV-App: 1) → connecting
 *   connecting ──a good selection read──▶ live
 *   connecting | live ──401 (link revoked or rotated)──▶ code (the token forgotten)
 *
 * The server address and the token are kept in localStorage (`storage.ts`).
 */
import type { TvPairCode } from "@/api/tvTypes";

export type PairPhase = "setup" | "connecting" | "code" | "live";

export interface PairState {
  phase: PairPhase;
  base: string | null;
  token: string | null;
  code: TvPairCode | null;
  /** the last call did not reach the server (it keeps trying) */
  offline: boolean;
  /** the stored link was refused (revoked from the portal): said above the code */
  revoked: boolean;
  /** a claimed code came back without the token: the address is not the app's entry */
  wrongEntry: boolean;
}

export type PairEvent =
  | { type: "configured"; base: string }
  | { type: "setup" }
  | { type: "live" }
  | { type: "unauthorized" }
  | { type: "code"; code: TvPairCode }
  | { type: "claimed"; token: string | null | undefined }
  | { type: "expired" }
  | { type: "offline" }
  | { type: "online" };

export function initialPair(stored: { base: string | null; token: string | null }): PairState {
  const base = stored.base;
  const token = base ? stored.token : null;
  return {
    phase: !base ? "setup" : token ? "connecting" : "code",
    base,
    token,
    code: null,
    offline: false,
    revoked: false,
    wrongEntry: false,
  };
}

export function pairReducer(s: PairState, e: PairEvent): PairState {
  switch (e.type) {
    case "configured": {
      // a new server: the old link belongs to the old one
      const token = e.base === s.base ? s.token : null;
      return { ...s, base: e.base, token, phase: token ? "connecting" : "code", code: null, offline: false, wrongEntry: false };
    }
    case "setup":
      return { ...s, phase: "setup", code: null };
    case "live":
      if (s.phase !== "connecting" && s.phase !== "live") return s;
      return { ...s, phase: "live", offline: false, revoked: false };
    case "unauthorized":
      if (s.phase !== "connecting" && s.phase !== "live") return s;
      return { ...s, phase: "code", token: null, code: null, revoked: s.token !== null };
    case "code":
      if (s.phase !== "code") return s;
      return { ...s, code: e.code, offline: false };
    case "claimed":
      if (s.phase !== "code") return s;
      if (!e.token) return { ...s, code: null, wrongEntry: true };
      return { ...s, phase: "connecting", token: e.token, code: null, offline: false, revoked: false, wrongEntry: false };
    case "expired":
      if (s.phase !== "code") return s;
      return { ...s, code: null };
    case "offline":
      return s.offline ? s : { ...s, offline: true };
    case "online":
      return s.offline ? { ...s, offline: false } : s;
    default:
      return s;
  }
}
