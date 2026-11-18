import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GoalParts } from "@/components/GoalParts";

const parts = [
  { portfolio_id: "a", name: "AGG", change_eur: "5.02", contribution_pct: "0.50" },
  { portfolio_id: "b", name: "PRU", change_eur: "0.00", contribution_pct: "0.00" },
  { portfolio_id: "c", name: "BIL", change_eur: "-1.20", contribution_pct: "-0.12" },
];

describe("GoalParts", () => {
  it("total goal: each portfolio's € and points, a still one without points", () => {
    render(<GoalParts goal={{ name: "settimanale", scope: "total", parts }} />);
    const line = screen.getByTestId("goal-parts-settimanale");
    expect(line).toHaveTextContent("AGG +5,02 € (+0,50 pt)· PRU 0,00 €· BIL -1,20 € (-0,12 pt)");
    expect(screen.getByText("+5,02 €")).toHaveClass("text-gain");
    expect(screen.getByText("-1,20 €")).toHaveClass("text-loss");
  });

  it("nothing for a portfolio goal, without parts or from an older server", () => {
    const { container } = render(
      <>
        <GoalParts goal={{ name: "a", scope: "portfolio", parts: parts.slice(0, 1) }} />
        <GoalParts goal={{ name: "b", scope: "total", parts: [] }} />
        <GoalParts goal={{ name: "c", scope: "total" }} />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
