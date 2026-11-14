import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LiveOrderConfirm } from "@/components/LiveOrderConfirm";

describe("LiveOrderConfirm", () => {
  it("real order confirm dialog for admin orders", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <LiveOrderConfirm
        portfolioName="Reale"
        order={{ symbol: "AAPL", side: "buy", type: "limit", qty: "3", limit_price: "180.5" }}
        onClose={() => undefined}
        onConfirm={onConfirm}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Ordine con denaro reale" });
    const summary = within(dialog).getByLabelText("Riepilogo dell'ordine");
    expect(summary).toHaveTextContent("PortafoglioReale");
    expect(summary).toHaveTextContent("SimboloAAPL");
    expect(summary).toHaveTextContent("LatoAcquisto");
    expect(summary).toHaveTextContent("TipoLimite");
    expect(summary).toHaveTextContent("Quantità3");
    expect(summary).toHaveTextContent("Prezzo limite180,5");
    expect(within(dialog).queryByLabelText(/TOTP/)).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/stop gestito da SMTrading/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Invia ordine reale" })); // one click
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("says that a trailing stop is managed by SMTrading, not by the broker", () => {
    render(
      <LiveOrderConfirm
        portfolioName="Reale"
        order={{ symbol: "AAPL", side: "sell", type: "trailing_stop", qty: "3", trail_percent: "2" }}
        onClose={() => undefined}
        onConfirm={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Ordine con denaro reale" });
    expect(within(dialog).getByRole("note")).toHaveTextContent("stop gestito da SMTrading, non dal broker");
    expect(within(dialog).getByLabelText("Riepilogo dell'ordine")).toHaveTextContent("Trailing2%");
  });
});
