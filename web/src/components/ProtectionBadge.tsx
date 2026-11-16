import type { Protection } from "@/api/types";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";

const PROTECTION: Record<Protection, { text: string; tone: StatusTone; hint: string }> = {
  ok: { text: "🟢 protetta", tone: "ok", hint: "stop attivo valido" },
  "scade oggi": { text: "🟠 scade oggi", tone: "warn", hint: "lo stop o il target è giornaliero: scade alla chiusura di oggi" },
  "senza stop": { text: "🔴 senza stop", tone: "danger", hint: "nessuno stop attivo sulla posizione" },
};

/** An open position's protection («Posizioni», dashboard). */
export function ProtectionBadge({ protection }: { protection: Protection }) {
  const b = PROTECTION[protection] ?? { text: protection, tone: "neutral" as const, hint: undefined };
  return (
    <StatusBadge tone={b.tone} title={b.hint}>
      {b.text}
    </StatusBadge>
  );
}
