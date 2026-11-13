import { useDashboardSummary } from "@/api/hooks/dashboard";
import { cn } from "@/lib/utils";

/** Global notice under the header: what kind of money the active portfolios are using. */
export function ModeBanner() {
  const { data } = useDashboardSummary();
  const modes = data?.modes;
  if (!modes) return null;
  const { simulazione = 0, paper = 0, reale = 0 } = modes;
  if (simulazione + paper + reale === 0) return null;

  let text: string;
  let tone: string;
  if (reale > 0) {
    text = "Attenzione: almeno un portafoglio opera con denaro reale";
    tone = "border-loss/40 bg-loss/15 text-loss";
  } else if (paper > 0) {
    text = "Simulazione e paper trading (conti demo) – nessun denaro reale";
    tone = "border-warn/40 bg-warn/15 text-warn";
  } else {
    text = "Simulazione – denaro virtuale, prezzi di mercato reali";
    tone = "border-gain/40 bg-gain/15 text-gain";
  }
  return (
    <div role="status" aria-label="Modalità di trading" className={cn("border-b px-6 py-1.5 text-center text-xs font-medium", tone)}>
      {text}
    </div>
  );
}
