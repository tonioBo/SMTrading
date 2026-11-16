import type { SignalDirection, SignalSymbol } from "@/api/types";

export type SignalTone = "now" | "closed" | "near" | "far";

/** «vicino»: |distance| within 20% of |threshold| (2% of the price for a crossing). */
export const NEAR_THRESHOLD_SHARE = 0.2;
export const NEAR_CROSS_PRICE_SHARE = 0.02;

const BADGES: Record<SignalTone, { icon: string; label: string }> = {
  now: { icon: "🟢", label: "scatterebbe ora" },
  closed: { icon: "🟢", label: "segnale sulla chiusura" },
  near: { icon: "🟡", label: "vicino" },
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

/** The status badge of a symbol (comparison only: displayed values stay strings). */
export function signalBadge(s: SignalSymbol): { tone: SignalTone; icon: string; label: string } {
  const tone = ((): SignalTone => {
    if (s.triggered_now) return "now";
    if (s.triggered_closed) return "closed";
    const distance = num(s.distance);
    const base = s.direction === "cross_up" ? num(s.last) : num(s.threshold);
    if (distance == null || base == null) return "far";
    const share = s.direction === "cross_up" ? NEAR_CROSS_PRICE_SHARE : NEAR_THRESHOLD_SHARE;
    return Math.abs(distance) <= Math.abs(base) * share + 1e-12 ? "near" : "far";
  })();
  return { tone, ...BADGES[tone] };
}

/** Share (0..100, rounded) of the weekly target reached: `pct` over `target_pct`. */
export function goalProgress(pct: string | null, target: string): number {
  const p = num(pct);
  const t = num(target);
  if (p == null || t == null || t <= 0) return 0;
  return Math.round(Math.min(100, Math.max(0, (p / t) * 100)));
}
