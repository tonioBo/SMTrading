import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { ACTIVITY_PAGE } from "@/api/hooks/activity";
import type { ActivityCatalog, ActivityRow } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const row = (id: number, over: Partial<ActivityRow> = {}): ActivityRow => ({
  id,
  ts: "2026-11-15T10:00:00Z",
  request_id: `r${id}`,
  user_id: "u1",
  email: "admin@example.com",
  role: "admin",
  action: "users.create",
  action_label: "Creazione utente",
  category: "users",
  category_label: "Utenti",
  method: "POST",
  route: "/api/users",
  target: {},
  outcome: "ok",
  status: 200,
  error_code: null,
  error_message: null,
  ip: "192.168.1.10",
  user_agent: "test",
  summary: { email: "anna@example.com", role: "viewer" },
  provisional: false,
  provisional_rows: 0,
  ...over,
});

const catalog: ActivityCatalog = {
  categories: [
    { key: "auth", label: "Accesso" },
    { key: "users", label: "Utenti" },
  ],
  actions: [
    { key: "users.create", label: "Creazione utente", category: "users" },
  ],
  outcomes: [{ key: "ok", label: "Riuscita" }],
};

let role = "admin";
let requests: URL[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () =>
    HttpResponse.json({
      email: "admin@example.com",
      totp_enabled: false,
      role,
      must_change_password: false,
    }),
  ),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/users", () =>
    HttpResponse.json([
      { id: "u1", email: "admin@example.com", role: "admin" },
      { id: "u2", email: "anna@example.com", role: "viewer" },
    ]),
  ),
  ...commonHandlers, // after /api/users: its empty list would win
  http.get("*/api/activity-log/catalog", () => HttpResponse.json(catalog)),
  http.get("*/api/activity-log", ({ request }) => {
    const url = new URL(request.url);
    requests.push(url);
    if (url.searchParams.get("before_id")) {
      return HttpResponse.json([
        row(1, {
          email: "ghost@example.com",
          user_id: null,
          role: null,
          action: "auth.login_failed",
          action_label: "Accesso non riuscito",
          outcome: "denied",
          status: 403,
          error_message: "credenziali non valide",
        }),
      ]);
    }
    return HttpResponse.json(
      Array.from({ length: ACTIVITY_PAGE }, (_, i) =>
        row(
          ACTIVITY_PAGE + 1 - i,
          i === 0
            ? {
                action: "users.set_role",
                action_label: "Cambio ruolo utente",
                target: { user_id: "u2" },
                outcome: "pending",
                status: null,
              }
            : {},
        ),
      ),
    );
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  requests = [];
  role = "admin";
});
afterAll(() => server.close());

describe("ActivityLog", () => {
  it("is in the sidebar for admins, in «Sistema» before Registro denaro reale", async () => {
    renderApp("/registro-attivita");
    const nav = await screen.findByRole("navigation", {
      name: "Navigazione principale",
    });
    const sistema = within(nav).getByRole("group", { name: "Sistema" });
    const labels = within(sistema)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(labels[labels.indexOf("Registro denaro reale") - 1]).toBe(
      "Registro attività",
    );
    expect(
      await screen.findByRole("heading", { name: "Registro attività" }),
    ).toBeInTheDocument();
  });

  it("table, filters, paging and the csv link", async () => {
    const user = userEvent.setup();
    renderApp("/registro-attivita");
    const table = await screen.findByRole("table", {
      name: "Registro attività",
    });
    expect(
      await within(table).findByText("Cambio ruolo utente"),
    ).toBeInTheDocument();
    expect(within(table).getByText("user_id: u2")).toBeInTheDocument();
    expect(within(table).getByText("In attesa dell'esito")).toBeInTheDocument();
    expect(within(table).getAllByText("Creazione utente").length).toBe(
      ACTIVITY_PAGE - 1,
    );
    expect(within(table).getAllByText("Amministratore").length).toBe(
      ACTIVITY_PAGE,
    );
    const csv = screen.getByRole("link", { name: "Esporta CSV" });
    expect(csv).toHaveAttribute("href", "/api/activity-log.csv");

    await user.click(screen.getByRole("button", { name: "Carica altri" }));
    expect(
      await within(table).findByText("Accesso non riuscito"),
    ).toBeInTheDocument();
    expect(within(table).getByText("Negata (403)")).toBeInTheDocument();
    expect(
      within(table).getByText("ghost@example.com (non autenticato)"),
    ).toBeInTheDocument();
    expect(requests.at(-1)!.searchParams.get("before_id")).toBe("2");
    expect(
      screen.queryByRole("button", { name: "Carica altri" }),
    ).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Utente"), "anna");
    await user.click(screen.getByLabelText("Solo anonimi"));
    await waitFor(() =>
      expect(
        within(screen.getByLabelText("Tipo di azione")).getAllByRole("option"),
      ).toHaveLength(3),
    );
    await user.selectOptions(screen.getByLabelText("Tipo di azione"), "auth");
    await user.selectOptions(screen.getByLabelText("Esito"), "denied");
    await user.type(screen.getByLabelText("Dal"), "2026-11-01");
    await user.type(screen.getByLabelText("Al"), "2026-11-15");
    await waitFor(() =>
      expect(requests.at(-1)!.searchParams.get("to")).toBe(
        "2026-11-15T23:59:59.999+01:00",
      ),
    );
    const last = requests.at(-1)!;
    expect(last.searchParams.get("email")).toBe("anna");
    expect(last.searchParams.get("anonymous")).toBe("true");
    expect(last.searchParams.get("category")).toBe("auth");
    expect(last.searchParams.get("outcome")).toBe("denied");
    expect(last.searchParams.get("from")).toBe("2026-11-01T00:00:00+01:00");
    expect(last.searchParams.get("limit")).toBe(String(ACTIVITY_PAGE));
    expect(csv.getAttribute("href")).toBe(
      "/api/activity-log.csv?email=anna&anonymous=true&category=auth&outcome=denied&from=2026-11-01T00%3A00%3A00%2B01%3A00&to=2026-11-15T23%3A59%3A59.999%2B01%3A00",
    );
  });

  it("a viewer has no sidebar entry and no request to the log", async () => {
    role = "viewer";
    renderApp("/registro-attivita");
    expect(
      await screen.findByText(
        "Solo gli amministratori possono vedere il registro attività.",
      ),
    ).toBeInTheDocument();
    const nav = await screen.findByRole("navigation", {
      name: "Navigazione principale",
    });
    await waitFor(() =>
      expect(
        within(nav).queryByText("Registro attività"),
      ).not.toBeInTheDocument(),
    );
    expect(requests).toEqual([]);
  });
});
