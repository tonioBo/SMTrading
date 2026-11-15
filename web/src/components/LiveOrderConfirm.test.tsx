import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { LiveOrderConfirm } from "@/components/LiveOrderConfirm";

const order = { symbol: "AAPL", side: "buy" as const, type: "limit", qty: "3", limit_price: "180.5", tif: "gtc", currency: "USD" };

describe("LiveOrderConfirm", () => {
  it("real order confirm dialog asks for the TOTP and shows the estimated amount", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <LiveOrderConfirm
        portfolioName="Reale"
        order={order}
        amount={{ value: "541.5", currency: "USD", eurEstimate: "498.2" }}
        onClose={() => undefined}
        onConfirm={onConfirm}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const summary = within(dialog).getByLabelText("Riepilogo dell'ordine");
    expect(summary).toHaveTextContent("PortafoglioReale");
    expect(summary).toHaveTextContent("SimboloAAPL");
    expect(summary).toHaveTextContent("LatoAcquisto");
    expect(summary).toHaveTextContent("TipoLimite");
    expect(summary).toHaveTextContent("Quantità3");
    expect(summary).toHaveTextContent("Prezzo limite180,5 USD");
    expect(summary).toHaveTextContent("Importo stimato541,50 $≈ 498,20 € (stima)");
    expect(summary).toHaveTextContent("DurataFino a revoca (gtc)");
    expect(within(dialog).queryByText(/stop gestito da SMTrading/)).not.toBeInTheDocument();
    const send = within(dialog).getByRole("button", { name: "Invia ordine reale" });
    expect(send).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "12345");
    expect(send).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "6");
    await user.click(send);
    expect(onConfirm).toHaveBeenCalledWith("123456");
  });

  it("totp error shown under field, and the code is cleared for the next one", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [error, setError] = useState<unknown>(null);
      return (
        <LiveOrderConfirm
          portfolioName="Reale"
          order={order}
          amount={null}
          error={error}
          onClose={() => undefined}
          onConfirm={() => setError(new ApiError(403, "FORBIDDEN", "codice TOTP già usato", { field: "totp" }))}
        />
      );
    }
    render(<Harness />);
    const dialog = screen.getByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const field = within(dialog).getByLabelText("Codice TOTP");
    await user.type(field, "123456");
    await user.click(within(dialog).getByRole("button", { name: "Invia ordine reale" }));
    expect(field).toHaveValue("");
    expect(field).toHaveAccessibleDescription("codice TOTP già usato");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument(); // only under the field
    expect(within(dialog).getByRole("button", { name: "Invia ordine reale" })).toBeDisabled();
  });

  it("clears the code on any server error but keeps it on a network error", async () => {
    const user = userEvent.setup();
    let next: unknown = new ApiError(0, "NETWORK_ERROR", "impossibile contattare il server");
    function Harness() {
      const [error, setError] = useState<unknown>(null);
      return (
        <LiveOrderConfirm portfolioName="Reale" order={order} amount={null} error={error} onClose={() => undefined} onConfirm={() => setError(next)} />
      );
    }
    render(<Harness />);
    const dialog = screen.getByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const field = within(dialog).getByLabelText("Codice TOTP");
    await user.type(field, "123456");
    await user.click(within(dialog).getByRole("button", { name: "Invia ordine reale" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("impossibile contattare il server");
    expect(field).toHaveValue("123456"); // the same code can be retried
    next = new ApiError(409, "BROKER_REJECTED", "rifiutato dal broker");
    await user.click(within(dialog).getByRole("button", { name: "Invia ordine reale" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("rifiutato dal broker");
    expect(field).toHaveValue(""); // the code may have been spent
  });

  it("says that a trailing stop is managed by SMTrading, not by the broker", () => {
    render(
      <LiveOrderConfirm
        portfolioName="Reale"
        order={{ symbol: "AAPL", side: "sell", type: "trailing_stop", qty: "3", trail_percent: "2" }}
        amount={null}
        onClose={() => undefined}
        onConfirm={() => undefined}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    expect(within(dialog).getByRole("note")).toHaveTextContent("stop gestito da SMTrading, non dal broker");
    expect(within(dialog).getByLabelText("Riepilogo dell'ordine")).toHaveTextContent("Trailing2%");
  });
});
