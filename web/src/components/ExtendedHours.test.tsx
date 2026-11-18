import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ExtendedPositionLine, ExtendedQuoteLine, GapRiskBadge } from "./ExtendedHours";

const now = new Date("2026-11-17T22:00:00Z");

describe("ExtendedHours", () => {
  it("quote line: pre-market and after-hours with the Rome time", () => {
    const { rerender } = render(<ExtendedQuoteLine now={now} ext={{ session: "post", price: "255.4", change_pct_vs_close: "-0.8", ts: "2026-11-17T21:45:00Z" }} />);
    expect(screen.getByLabelText("Prezzo fuori orario")).toHaveTextContent("🌙 After-hours 255,40 $ (-0,8%) · 22:45");
    rerender(<ExtendedQuoteLine now={now} ext={{ session: "pre", price: "259.99", change_pct_vs_close: "0.98", ts: "2026-11-17T11:45:00Z" }} />);
    expect(screen.getByLabelText("Prezzo fuori orario")).toHaveTextContent("🌅 Pre-market 259,99 $ (+1,0%) · 12:45");
  });

  it("renders nothing without an extended-hours price", () => {
    const { container } = render(
      <>
        <ExtendedQuoteLine ext={null} />
        <ExtendedPositionLine ext={undefined} currency="USD" />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("position line and gap-risk badge", () => {
    render(
      <>
        <ExtendedPositionLine currency="USD" ext={{ session: "post", price: "240", change_pct_vs_close: "-8.54", ts: "2026-11-17T21:45:00Z" }} />
        <GapRiskBadge side="long" />
      </>,
    );
    expect(screen.getByText(/^fuori orario/)).toHaveTextContent("fuori orario 240,00 $ (-8,5%)");
    expect(screen.getByText("⚠ sotto lo stop fuori orario: rischio gap all'apertura")).toHaveClass("text-warn");
  });
});
