import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GoalBar } from "@/components/GoalBar";

function fill() {
  return screen.getByRole("progressbar").firstElementChild as HTMLElement;
}

describe("GoalBar", () => {
  it("risk goal: the loss over the limit, amber near it, red at it", () => {
    const { rerender } = render(<GoalBar pct="1" target="5" kind="max_loss" label="x" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "20");
    expect(fill()).toHaveClass("bg-accent");
    rerender(<GoalBar pct="4" target="5" kind="max_loss" label="x" />);
    expect(fill()).toHaveClass("bg-warn");
    rerender(<GoalBar pct="5" target="5" kind="max_loss" label="x" />);
    expect(fill()).toHaveClass("bg-loss");
  });

  it("the elapsed-time marker: drawn for a minimum, never for a risk goal", () => {
    const { container, rerender } = render(<GoalBar pct="1" target="2" kind="min" elapsed="50" label="x" />);
    expect(container.querySelector('[title="tempo trascorso"]')).not.toBeNull();
    rerender(<GoalBar pct="1" target="5" kind="max_loss" elapsed="50" label="x" />);
    expect(container.querySelector('[title="tempo trascorso"]')).toBeNull();
  });

  it("minimum reached is green, cap exceeded is red", () => {
    const { rerender } = render(<GoalBar pct="2" target="2" kind="min" label="x" />);
    expect(fill()).toHaveClass("bg-gain");
    rerender(<GoalBar pct="6" target="5" kind="cap" label="x" />);
    expect(fill()).toHaveClass("bg-loss");
  });
});
