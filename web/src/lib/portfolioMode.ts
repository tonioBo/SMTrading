import type { PortfolioMode } from "@/api/types";

/**
 * Colours and hints per mode. The text shown is the backend's `mode_label` (with the
 * broker's name); `label` is only the fallback for a response without it.
 */
export const MODE_INFO: Record<PortfolioMode, { label: string; hint: string; className: string }> = {
  simulazione: {
    label: "SMTrading (simulato)",
    hint: "Denaro virtuale, prezzi di mercato reali",
    className: "border-gain/40 bg-gain/15 text-gain",
  },
  paper: {
    label: "Conto di prova del broker",
    hint: "Conto di prova del broker, nessun denaro reale",
    className: "border-warn/40 bg-warn/15 text-warn",
  },
  reale: {
    label: "🔴 Reale",
    hint: "Denaro reale",
    className: "border-loss/40 bg-loss/15 text-loss",
  },
};
