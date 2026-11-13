import type { PortfolioMode } from "@/api/types";

export const MODE_INFO: Record<PortfolioMode, { icon: string; label: string; hint: string; className: string }> = {
  simulazione: {
    icon: "🟢",
    label: "SIMULAZIONE",
    hint: "Denaro virtuale, prezzi di mercato reali",
    className: "border-gain/40 bg-gain/15 text-gain",
  },
  paper: {
    icon: "🟡",
    label: "PAPER",
    hint: "Conto demo del broker, nessun denaro reale",
    className: "border-warn/40 bg-warn/15 text-warn",
  },
  reale: {
    icon: "🔴",
    label: "REALE",
    hint: "Denaro reale",
    className: "border-loss/40 bg-loss/15 text-loss",
  },
};
