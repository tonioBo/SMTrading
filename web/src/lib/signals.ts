import type { SignalDirection, SignalExit, SignalsResponse, SignalSymbol } from "@/api/types";

export type SignalTone = "held" | "pending" | "now" | "closed" | "near" | "above" | "far";

/** «vicino»: 0 < distance <= 20% of |threshold| (2% of the price for a crossing): still to
 * go, but close. At or past the threshold without a signal is never «vicino». */
export const NEAR_THRESHOLD_SHARE = 0.2;
export const NEAR_CROSS_PRICE_SHARE = 0.02;

const BADGES: Record<SignalTone, { icon: string; label: string }> = {
  held: { icon: "📌", label: "in posizione" },
  pending: { icon: "⏳", label: "ordine in attesa" },
  now: { icon: "🟢", label: "scatterebbe ora" },
  closed: { icon: "🟢", label: "segnale sulla chiusura" },
  near: { icon: "🟡", label: "vicino" },
  above: { icon: "⚪", label: "già sopra · attende un nuovo incrocio" },
  far: { icon: "⚪", label: "lontano" },
};

export const DIRECTION_LABELS: Record<SignalDirection, string> = {
  below: "sotto",
  above: "sopra",
  cross_up: "incrocio al rialzo di",
};

function num(value: string | null | undefined): number | null {
  if (value == null || value.trim() === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** The status badge of a symbol (comparison only: displayed values stay strings). An open
 * position, then a resting entry order, take precedence over the indicator's signal. */
export function signalBadge(s: SignalSymbol): { tone: SignalTone; icon: string; label: string } {
  const tone = ((): SignalTone => {
    if (s.position) return "held";
    if (s.open_entry) return "pending";
    if (s.triggered_now) return "now";
    if (s.triggered_closed) return "closed";
    const distance = num(s.distance);
    const base = s.direction === "cross_up" ? num(s.last) : num(s.threshold);
    if (distance == null || base == null) return "far";
    // cross_up at or past it without a fresh cross: the fast average is already above
    if (s.direction === "cross_up" && distance <= 0) return "above";
    const share = s.direction === "cross_up" ? NEAR_CROSS_PRICE_SHARE : NEAR_THRESHOLD_SHARE;
    return distance > 0 && distance <= Math.abs(base) * share + 1e-12 ? "near" : "far";
  })();
  return { tone, ...BADGES[tone] };
}

export interface ClosestSignal {
  portfolioId: string | null;
  portfolioName: string | null;
  ref: string;
  name: string;
  symbol: SignalSymbol;
}

/** How far a row still is, as a share of its «vicino» band (<= 1: vicino); fired rows -1;
 * null when it is not a candidate (in position, pending, already above, no data). */
function closeness(s: SignalSymbol): number | null {
  const tone = signalBadge(s).tone;
  if (tone === "now" || tone === "closed") return -1;
  if (tone !== "near" && tone !== "far") return null;
  const distance = num(s.distance);
  const base = s.direction === "cross_up" ? num(s.last) : num(s.threshold);
  if (distance == null || base == null || distance <= 0 || base === 0) return null;
  const share = s.direction === "cross_up" ? NEAR_CROSS_PRICE_SHARE : NEAR_THRESHOLD_SHARE;
  return distance / (Math.abs(base) * share);
}

/** The `n` symbols closest to an entry across every portfolio and strategy (the
 * dashboard card): fired ones first, then by distance relative to the «vicino» band. */
export function closestSignals(resp: SignalsResponse | undefined, n: number): ClosestSignal[] {
  const out: { item: ClosestSignal; c: number }[] = [];
  for (const p of resp?.portfolios ?? []) {
    for (const st of p.strategies) {
      for (const symbol of st.symbols) {
        const c = closeness(symbol);
        if (c != null) out.push({ item: { portfolioId: p.portfolio_id, portfolioName: p.portfolio_name, ref: st.ref, name: st.name, symbol }, c });
      }
    }
  }
  return out
    .sort((a, b) => a.c - b.c)
    .slice(0, n)
    .map((x) => x.item);
}

export type ExitTone = "fires" | "near";

const EXIT_BADGES: Record<ExitTone, { icon: string; label: string }> = {
  fires: { icon: "🔻", label: "uscita scatterebbe" },
  near: { icon: "🟡", label: "uscita vicina" },
};

/** The exit rule's badge while in position: it fires, it is near (0 < distance <= 20% of
 * the RSI threshold, 2% of the price threshold for sma_cross / breakout), else none. */
export function exitBadge(type: string | null, exit: SignalExit | null | undefined): { tone: ExitTone; icon: string; label: string } | null {
  if (!exit) return null;
  const tone = ((): ExitTone | null => {
    if (exit.triggered) return "fires";
    const distance = num(exit.distance);
    const threshold = num(exit.threshold);
    if (distance == null || threshold == null || distance <= 0) return null;
    const share = type === "rsi_reversion" ? NEAR_THRESHOLD_SHARE : NEAR_CROSS_PRICE_SHARE;
    return distance <= Math.abs(threshold) * share + 1e-12 ? "near" : null;
  })();
  return tone ? { tone, ...EXIT_BADGES[tone] } : null;
}

/** Share (0..100, rounded) of the weekly target reached: `pct` over `target_pct`. */
export function goalProgress(pct: string | null, target: string): number {
  const p = num(pct);
  const t = num(target);
  if (p == null || t == null || t <= 0) return 0;
  return Math.round(Math.min(100, Math.max(0, (p / t) * 100)));
}
