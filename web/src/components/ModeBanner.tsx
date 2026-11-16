import { useModes } from "@/api/hooks/dashboard";
import { useLiveStatus } from "@/api/hooks/live";
import { TELEGRAM_DOWN_TEXT } from "@/lib/live";
import { cn } from "@/lib/utils";

/**
 * Global notice under the header: what kind of money the active portfolios are using.
 * With a real-money link armed there is no bar (the header shows a red-dot badge), only a
 * fixed warning if Telegram is not working: real orders go on, their notifications wait in the queue.
 */
export function ModeBanner() {
  const { data: modes, isError } = useModes();
  const { data: live } = useLiveStatus();

  if (live && live.armed_count > 0) {
    // the red-dot badge in the header (LiveMoneyIndicator) says it; here only the Telegram warning
    if (live.telegram_ok) return null;
    return (
      <div role="alert" className="border-b border-warn/40 bg-warn/15 px-6 py-1.5 text-center text-xs font-semibold text-warn">
        {TELEGRAM_DOWN_TEXT}
      </div>
    );
  }

  if (!modes) {
    // fail safe: never silently hide what kind of money is in use
    if (!isError) return null;
    return (
      <div role="status" aria-label="Modalità di trading" className="border-b border-border bg-card px-6 py-1.5 text-center text-xs font-medium text-muted">
        Modalità dei portafogli non verificabile al momento
      </div>
    );
  }
  const { simulazione = 0, paper = 0, reale = 0 } = modes;
  if (simulazione + paper + reale === 0) return null;

  let text: string;
  let tone: string;
  if (reale > 0) {
    text = "Attenzione: almeno un portafoglio opera con denaro reale";
    tone = "border-loss/40 bg-loss/15 text-loss";
  } else if (paper > 0) {
    text = "SMTrading (simulato) e conti di prova dei broker – nessun denaro reale";
    tone = "border-warn/40 bg-warn/15 text-warn";
  } else {
    text = "SMTrading (simulato) – denaro virtuale, prezzi di mercato reali";
    tone = "border-gain/40 bg-gain/15 text-gain";
  }
  return (
    <div role="status" aria-label="Modalità di trading" className={cn("border-b px-6 py-1.5 text-center text-xs font-medium", tone)}>
      {text}
    </div>
  );
}
