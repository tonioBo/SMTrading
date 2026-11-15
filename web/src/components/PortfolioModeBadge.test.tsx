import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";

describe("PortfolioModeBadge", () => {
  it.each([
    ["SMTrading (simulato)", "simulazione", null, "text-gain"],
    ["Trading 212 · demo", "paper", null, "text-warn"],
    ["Alpaca · prova", "paper", null, "text-warn"],
    ["🔴 Alpaca · reale · sospeso", "paper", "configured", "text-loss"],
    ["🔴 eToro · reale · prova a secco", "paper", "dry_run", "text-loss"],
    ["🔴 Trading 212 · reale", "reale", "armed", "text-loss"],
  ] as const)("badge shows broker label: %s (%s, %s) → %s", (label, mode, liveState, tone) => {
    render(<PortfolioModeBadge mode={mode} liveState={liveState} label={label} />);
    const el = screen.getByLabelText(new RegExp(`^Modalità: ${label.replace(/[()]/g, "\\$&")}\\. `));
    expect(el.textContent).toBe(label);
    expect(el.className).toContain(tone);
  });

  it.each([
    ["simulazione", "SMTrading (simulato)", "text-gain"],
    ["paper", "Conto di prova del broker", "text-warn"],
    ["reale", "🔴 Reale", "text-loss"],
  ] as const)("falls back without a label: %s → %s", (mode, label, tone) => {
    render(<PortfolioModeBadge mode={mode} />);
    const el = screen.getByLabelText(new RegExp(`^Modalità: ${label.replace(/[()]/g, "\\$&")}\\. `));
    expect(el.textContent).toBe(label);
    expect(el.className).toContain(tone);
  });

  it.each([
    ["armed", "Denaro reale: gli ordini vanno al conto vero"],
    ["dry_run", "Prova a secco: gli ordini si fermano prima dell'invio"],
    ["configured", "Collegamento reale sospeso: nessun ordine viene inviato"],
  ] as const)("a real-money link is red with its live hint whatever the mode: %s", (liveState, hint) => {
    render(<PortfolioModeBadge mode="paper" liveState={liveState} label="🔴 Alpaca · reale" />);
    const el = screen.getByLabelText(`Modalità: 🔴 Alpaca · reale. ${hint}`);
    expect(el).toHaveAttribute("title", hint);
    expect(el.className).toContain("text-loss");
  });
});
