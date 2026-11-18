import type { SignalSymbol } from "@/api/types";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { formatQty } from "@/lib/format";
import { exitBadge, signalBadge, type ExitTone, type SignalTone } from "@/lib/signals";

const SIGNAL_TONE: Record<SignalTone, StatusTone> = {
  held: "info",
  pending: "info",
  now: "ok",
  closed: "ok",
  blocked: "warn",
  near: "warn",
  above: "neutral",
  far: "neutral",
};

const EXIT_TONE: Record<ExitTone, StatusTone> = { fires: "danger", near: "warn" };

/** The entry status of a signal row («Segnali», dashboard). */
export function SignalBadge({ s, type }: { s: SignalSymbol; type?: string | null }) {
  const b = signalBadge(s, type);
  return (
    <StatusBadge tone={SIGNAL_TONE[b.tone]} title={s.position ? `${formatQty(s.position.qty)} @ ${formatQty(s.position.avg_price)}` : undefined}>
      {`${b.icon} ${b.label}`}
    </StatusBadge>
  );
}

/** The exit rule's badge while in position (nothing when the exit is not close). */
export function ExitBadge({ type, s }: { type: string | null; s: SignalSymbol }) {
  const b = exitBadge(type, s.exit);
  return b ? <StatusBadge tone={EXIT_TONE[b.tone]}>{`${b.icon} ${b.label}`}</StatusBadge> : null;
}
