import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { safeNext } from "@/lib/nav";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const logins: unknown[] = [];
const server = setupServer(
  ...commonHandlers,
  http.post("*/api/auth/login", async ({ request }) => {
    const body = (await request.json()) as { totp?: string };
    logins.push(body);
    if (!body.totp) {
      return HttpResponse.json(
        { code: "FORBIDDEN", message: "codice TOTP richiesto", details: { totp_required: true } },
        { status: 403 },
      );
    }
    if (body.totp !== "123456") {
      return HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP non valido", details: null }, { status: 403 });
    }
    return HttpResponse.json({ email: "admin@example.com", totp_enabled: true });
  }),
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: true })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agents", () => HttpResponse.json([])),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  logins.length = 0;
  server.resetHandlers();
});
afterAll(() => server.close());

describe("Login", () => {
  it("asks for the TOTP code when required, then goes to ?next", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/login?next=%2Fagenti");
    await user.type(screen.getByLabelText("Email"), "admin@example.com");
    await user.type(screen.getByLabelText("Password"), "segreta");
    expect(screen.queryByLabelText("Codice TOTP")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accedi" }));

    expect(await screen.findByLabelText("Codice TOTP")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Inserisci il codice TOTP");

    await user.type(screen.getByLabelText("Codice TOTP"), "000000");
    await user.click(screen.getByRole("button", { name: "Accedi" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("codice TOTP non valido");

    await user.clear(screen.getByLabelText("Codice TOTP"));
    await user.type(screen.getByLabelText("Codice TOTP"), "123456");
    await user.click(screen.getByRole("button", { name: "Accedi" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/agenti"));
    expect(logins).toEqual([
      { email: "admin@example.com", password: "segreta" },
      { email: "admin@example.com", password: "segreta", totp: "000000" },
      { email: "admin@example.com", password: "segreta", totp: "123456" },
    ]);
    expect(await screen.findByRole("heading", { name: "Agenti" })).toBeInTheDocument();
  });

  it("shows the API message on bad credentials", async () => {
    server.use(
      http.post("*/api/auth/login", () =>
        HttpResponse.json({ code: "FORBIDDEN", message: "credenziali non valide", details: null }, { status: 403 }),
      ),
    );
    const user = userEvent.setup();
    renderApp("/login");
    await user.type(screen.getByLabelText("Email"), "a@b.it");
    await user.type(screen.getByLabelText("Password"), "x");
    await user.click(screen.getByRole("button", { name: "Accedi" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("credenziali non valide");
    expect(screen.queryByLabelText("Codice TOTP")).not.toBeInTheDocument();
  });
});

describe("safeNext", () => {
  it("accepts only in-app paths", () => {
    expect(safeNext("/portafogli/1?x=2")).toBe("/portafogli/1?x=2");
    expect(safeNext(null)).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
    expect(safeNext("/login")).toBe("/");
  });
});
