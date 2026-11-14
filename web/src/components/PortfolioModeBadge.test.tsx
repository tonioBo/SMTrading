import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";

describe("PortfolioModeBadge", () => {
  it.each([
    ["simulazione", "SIMULAZIONE", "text-gain"],
    ["paper", "PAPER", "text-warn"],
    ["reale", "REALE", "text-loss"],
  ] as const)("renders %s with text, accessible name and theme tokens", (mode, label, tone) => {
    render(<PortfolioModeBadge mode={mode} />);
    const el = screen.getByLabelText(new RegExp(`Modalità: ${label.toLowerCase()}\\.`));
    expect(el).toHaveTextContent(label);
    expect(el.className).toContain(tone);
  });

  it.each([
    ["armed", "REALE", "paper"],
    ["dry_run", "REALE · PROVA", "paper"],
    ["configured", "REALE · SOSPESO", "paper"],
  ] as const)("badge variants per live_state: %s → 🔴 %s, red even when the mode is %s", (liveState, label, mode) => {
    render(<PortfolioModeBadge mode={mode} liveState={liveState} />);
    const el = screen.getByLabelText(new RegExp(`^Modalità: ${label.toLowerCase()}\\.`));
    expect(el).toHaveTextContent(`🔴${label}`);
    expect(el.className).toContain("text-loss");
  });
});
