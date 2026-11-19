import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { LineChart, type ChartLine } from "@/components/LineChart";
import { resetViewport, setViewportWidth } from "@/test/viewport";

const T = Date.UTC(2026, 10, 16, 9);
const H = 3_600_000;
const pct = (v: number) => `${v}%`;
const series = (key: string, vs: number[]): ChartLine => ({
  key, label: key, color: "red", points: vs.map((v, i) => ({ t: T + i * H, v })),
});
const goal = (v: number): ChartLine => ({
  key: "goal", label: `Obiettivo +${v}%`, tag: `+${v}%`, color: "green", reference: true,
  points: [{ t: T, v }, { t: T + 3 * H, v }],
});

function chart(lines: ChartLine[]) {
  render(<LineChart lines={lines} formatValue={pct} formatTick={pct} axisTime="day" ariaLabel="Andamento" />);
}

describe("LineChart scale", () => {
  it("zooms on the series: a goal above them becomes an edge label, not a line", () => {
    chart([series("AGG", [0, 0.4, 1, 0.9]), goal(2)]);
    expect(screen.getByTestId("edge-goal")).toHaveTextContent("+2% ↑");
    expect(screen.queryByTestId("line-goal")).toBeNull();
    expect(screen.queryByText("2%")).toBeNull(); // no tick at the goal: the scale ends near 1%
  });

  it("draws a goal inside the series' range as a dashed line", () => {
    chart([series("AGG", [0, 1.5, 2.5, 2.2]), goal(2)]);
    expect(screen.getByTestId("line-goal")).toHaveAttribute("stroke-dasharray", "6 4");
    expect(screen.queryByTestId("edge-goal")).toBeNull();
  });

  it("marks a goal below the series with a down arrow", () => {
    chart([series("AGG", [3, 4, 5, 4.5]), goal(2)]);
    expect(screen.getByTestId("edge-goal")).toHaveTextContent("+2% ↓");
  });
});

describe("LineChart width", () => {
  afterEach(() => resetViewport());

  it("fills its box and shows fewer time labels on a phone", () => {
    setViewportWidth(1280);
    const { unmount } = render(<LineChart lines={[series("AGG", [0, 1, 2, 3])]} formatValue={pct} formatTick={pct} axisTime="day" ariaLabel="Andamento" />);
    expect(screen.getByTestId("line-chart").className).toContain("w-full");
    expect(screen.getByTestId("x-labels").children).toHaveLength(5);
    unmount();
    setViewportWidth(375);
    chart([series("AGG", [0, 1, 2, 3])]);
    expect(screen.getByTestId("x-labels").children).toHaveLength(3);
  });
});
