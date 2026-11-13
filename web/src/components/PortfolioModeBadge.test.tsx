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
});
