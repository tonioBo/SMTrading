import { fireEvent, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers, HEALTH_OK } from "@/test/handlers";
import { resetViewport, setViewportWidth } from "@/test/viewport";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const PROBLEM = {
  ...HEALTH_OK,
  status: "problema",
  reasons: ["broker sync: si sta riavviando dopo un errore", "2 quotazioni vecchie (us_equity): MSFT, NVDA", "1 cicli agenti attesi e non partiti oggi"],
  sections: {
    ...HEALTH_OK.sections,
    services: {
      status: "problema",
      label: "Servizi",
      summary: "broker sync: si sta riavviando dopo un errore",
      reasons: ["broker sync: si sta riavviando dopo un errore"],
      heartbeat_age_s: 4,
      jobs: [
        { name: "broker sync", status: "problema", note: "si sta riavviando dopo un errore", started_at: "2026-11-19T09:59:00Z", last_tick_at: null, interval_s: null, crashes: 3, last_crash_at: "2026-11-19T09:59:55Z", last_error: "RuntimeError: boom" },
        { name: "paperclip reader", status: "ok", note: null, started_at: "2026-11-19T08:00:00Z", last_tick_at: "2026-11-19T09:58:00Z", interval_s: 300, crashes: 0, last_crash_at: null, last_error: null },
      ],
    },
    quotes: {
      status: "attenzione",
      label: "Quotazioni",
      summary: "2 quotazioni vecchie su 3 simboli",
      reasons: ["2 quotazioni vecchie (us_equity): MSFT, NVDA"],
      classes: [{ asset_class: "us_equity", symbols: 3, fresh: 1, stale: 2, closed: 0, threshold_s: 120, stale_symbols: ["MSFT", "NVDA"] }],
    },
    cycles: {
      status: "attenzione",
      label: "Cicli agenti di oggi",
      summary: "1 cicli partiti su 2 attesi finora (3 nella giornata)",
      reasons: ["1 cicli agenti attesi e non partiti oggi"],
      cycles: {
        day: "2026-11-19", read_at: "2026-11-19T09:58:00Z", error: null, expected: 2, day_total: 3, done: 1, missing: 1, invalid_cron: [],
        routines: [{ routine: "Ciclo mattina", agent: "Portfolio Manager", expected: 2, day_total: 3, done: 1, completed: 1 }],
      },
    },
  },
};

let body: object = HEALTH_OK;
const server = setupServer(
  http.get("*/api/system-health", () => HttpResponse.json(body as Record<string, unknown>)), // before the common «all ok» one
  ...commonHandlers,
  http.get("*/api/dashboard", () => new HttpResponse(null, { status: 503 })),
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  body = HEALTH_OK;
  resetViewport();
});
afterAll(() => server.close());

describe("SystemHealth", () => {
  it("shows every section ok and the version (a viewer reads it)", async () => {
    renderApp("/salute-sistema");
    expect(await screen.findByRole("heading", { name: "Salute del sistema" })).toBeInTheDocument();
    const overall = await screen.findByRole("region", { name: "Stato complessivo" });
    expect(overall).toHaveTextContent("Tutto ok");
    for (const name of ["Servizi", "Fornitori di dati", "Quotazioni", "Lettura di Paperclip", "Cicli agenti di oggi", "Passaggi mancanti", "Telegram", "Costi agenti", "Errori MCP"]) {
      expect(screen.getByRole("region", { name })).toHaveTextContent("Tutto ok");
    }
    expect(screen.getByRole("region", { name: "Versione" })).toHaveTextContent("abc123");
  });

  it("lists the reasons, the failing job and the stale quotes", async () => {
    body = PROBLEM;
    renderApp("/salute-sistema");
    const overall = await screen.findByRole("region", { name: "Stato complessivo" });
    expect(overall).toHaveTextContent("Problema");
    expect(within(overall).getAllByRole("listitem")).toHaveLength(3);
    const services = screen.getByRole("region", { name: "Servizi" });
    const job = within(services).getByRole("row", { name: /Sincronizzazione broker/ });
    expect(job).toHaveTextContent("RuntimeError: boom");
    expect(job).toHaveTextContent("3");
    const quotes = screen.getByRole("region", { name: "Quotazioni" });
    expect(within(quotes).getByRole("row", { name: /Azioni USA/ })).toHaveTextContent("2");
    const cycles = screen.getByRole("region", { name: "Cicli agenti di oggi" });
    expect(cycles).toHaveTextContent("1 cicli partiti su 2 attesi finora (3 nella giornata)");
    expect(within(cycles).getByRole("row", { name: /Ciclo mattina/ })).toHaveTextContent("Portfolio Manager");
  });

  it("shows the automatic backups and the restore test", async () => {
    body = {
      ...HEALTH_OK,
      status: "attenzione",
      reasons: ["prova di ripristino non riuscita: nessuna riga in fills"],
      sections: {
        ...HEALTH_OK.sections,
        backup: {
          status: "attenzione",
          label: "Backup",
          summary: "prova di ripristino non riuscita: nessuna riga in fills",
          reasons: ["prova di ripristino non riuscita: nessuna riga in fills"],
          last_ok_at: "2026-11-19T02:31:00Z",
          last_ok_name: "smtrading-20261119-0330.dump",
          last_backup: { kind: "backup", status: "ok", trigger: "programmato", started_at: "2026-11-19T02:30:00Z", duration_s: 12.5, name: "smtrading-20261119-0330.dump", size_bytes: 2048, error: null },
          last_restore_test: { kind: "restore_test", status: "non riuscito", trigger: "programmato", started_at: "2026-11-16T03:00:00Z", name: "smtrading-20261116-0330.dump", size_bytes: 2048, error: "nessuna riga in fills" },
          destinations: [
            { dir: "/backups/local", files: 12, error: null },
            { dir: "/backups/onedrive", files: 0, error: "cartella non disponibile" },
          ],
        },
      },
    };
    renderApp("/salute-sistema");
    const backup = await screen.findByRole("region", { name: "Backup" });
    expect(backup).toHaveTextContent("Attenzione");
    expect(backup).toHaveTextContent("smtrading-20261119-0330.dump");
    expect(backup).toHaveTextContent("non riuscito");
    const dirs = within(backup).getByRole("list", { name: "Cartelle dei backup" });
    expect(within(dirs).getAllByRole("listitem")[0]).toHaveTextContent("12 backup");
    expect(within(dirs).getAllByRole("listitem")[1]).toHaveTextContent("cartella non disponibile");
  });

  it("stacks the tables as cards on a phone", async () => {
    body = PROBLEM;
    setViewportWidth(390);
    renderApp("/salute-sistema");
    const services = await screen.findByRole("region", { name: "Servizi" });
    expect(within(services).queryByRole("table")).not.toBeInTheDocument();
    const jobs = within(services).getByRole("list", { name: "Lavori del worker" });
    expect(within(jobs).getAllByRole("listitem")[0]).toHaveTextContent("Sincronizzazione broker");
  });

  it("is the first item of the «Sistema» group and a badge on the Dashboard", async () => {
    body = PROBLEM;
    renderApp("/");
    const badge = await screen.findByRole("link", { name: "Salute del sistema: Problema" });
    expect(badge).toHaveAttribute("href", "/salute-sistema");
    expect(badge).toHaveAttribute("title", "broker sync: si sta riavviando dopo un errore (e altri 2)");
    const nav = screen.getByRole("navigation", { name: "Navigazione principale" });
    const sistema = within(nav).getByRole("group", { name: "Sistema" });
    fireEvent.click(within(sistema).getByRole("button", { name: "Sistema" })); // the group starts closed
    expect(within(sistema).getAllByRole("link")[0]).toHaveTextContent("Salute del sistema");
  });
});

describe("SystemHealth scalper job", () => {
  it("shows the scalper with its active rules and the «fermato» state", async () => {
    body = {
      ...HEALTH_OK,
      sections: {
        ...HEALTH_OK.sections,
        services: {
          ...HEALTH_OK.sections.services,
          jobs: [
            { name: "scalper", status: "ok", note: null, started_at: "2026-11-19T09:00:00Z", last_tick_at: "2026-11-19T09:59:50Z", interval_s: 30, crashes: 0, last_crash_at: null, last_error: null, info: { active_rules: 3 } },
            { name: "broker sync", status: "ok", note: "fermato", started_at: "2026-11-19T09:00:00Z", last_tick_at: null, interval_s: null, crashes: 0, last_crash_at: null, last_error: null },
          ],
        },
      },
    };
    renderApp("/salute-sistema");
    const services = await screen.findByRole("region", { name: "Servizi" });
    expect(await within(services).findByText("Regole automatiche")).toBeInTheDocument();
    expect(within(services).getByText("3 regole attive")).toBeInTheDocument();
    expect(within(services).getByText("fermato")).toBeInTheDocument();
  });

  it("says «nessuna regola attiva» for a scalper with none", async () => {
    body = {
      ...HEALTH_OK,
      sections: {
        ...HEALTH_OK.sections,
        services: { ...HEALTH_OK.sections.services, jobs: [{ name: "scalper", status: "ok", note: "fermato", started_at: "2026-11-19T09:00:00Z", last_tick_at: null, interval_s: 30, crashes: 0, last_crash_at: null, last_error: null, info: { active_rules: 0 } }] },
      },
    };
    renderApp("/salute-sistema");
    const services = await screen.findByRole("region", { name: "Servizi" });
    expect(await within(services).findByText("fermato · nessuna regola attiva")).toBeInTheDocument();
  });
});

describe("SystemHealth sentinel", () => {
  it("shows the sentinel's open problems and last pass, outside the overall status", async () => {
    body = {
      ...HEALTH_OK,
      sentinel: {
        status: "attenzione", label: "Sentinella di sistema", summary: "1 problema aperto",
        reasons: ["Agente Risk Manager in errore"], on: true, last_pass_at: "2026-11-19T09:58:00Z", last_error: null, alerts_7d: 4,
        open: [{ key: "agent:a1", kind: "agent", label: "Agenti Paperclip", title: "Agente Risk Manager in errore", detail: "adapter crashed", level: "warning", since: "2026-11-19T09:30:00Z" }],
      },
    };
    renderApp("/salute-sistema");
    const sentinel = await screen.findByRole("region", { name: "Sentinella di sistema" });
    expect(sentinel).toHaveTextContent("Attenzione");
    expect(sentinel).toHaveTextContent("1 problema aperto");
    const open = within(sentinel).getByRole("list", { name: "Problemi aperti della sentinella" });
    expect(within(open).getByRole("listitem")).toHaveTextContent("Agente Risk Manager in errore");
    expect(within(open).getByRole("listitem")).toHaveTextContent("adapter crashed");
    expect(within(open).getByRole("listitem")).toHaveTextContent("Agenti Paperclip");
    expect(sentinel).toHaveTextContent("Avvisi negli ultimi 7 giorni4");
    expect(screen.getByRole("region", { name: "Stato complessivo" })).toHaveTextContent("Tutto ok");
  });

  it("is absent on an older api", async () => {
    renderApp("/salute-sistema");
    await screen.findByRole("region", { name: "Stato complessivo" });
    expect(screen.queryByRole("region", { name: "Sentinella di sistema" })).not.toBeInTheDocument();
  });
});

describe("SystemHealth price quality and quotas", () => {
  it("shows the suspect quotes and the providers' quota bars", async () => {
    body = {
      ...HEALTH_OK,
      status: "attenzione",
      reasons: ["NVDA: prezzo non confermato da 16 min"],
      sections: {
        ...HEALTH_OK.sections,
        price_quality: {
          status: "attenzione", label: "Qualità dei prezzi", summary: "NVDA: prezzo non confermato da 16 min",
          reasons: ["NVDA: prezzo non confermato da 16 min"], suspects_1h: 3, suspects_24h: 7,
          top_symbols: [{ symbol: "NVDA", count: 5 }], by_reason: [{ reason: "jump", label: "salto improvviso", count: 6 }],
          by_provider: [], current: [{ symbol: "NVDA", since: "2026-11-19T09:44:00Z", age_s: 960 }],
        },
        quotas: {
          status: "attenzione", label: "Quota delle fonti", summary: "twelvedata: quota giornaliera al 95%",
          reasons: ["twelvedata: quota giornaliera al 95%, usato per ultimo fino al reset"], note: "da verificare",
          providers: [{ name: "twelvedata", quota: {
            per_min: 8, per_hour: null, per_day: 800, used_minute: 0, used_hour: 10, used_day: 760, pct_minute: 0, pct_hour: null,
            pct_day: 95, demoted: true, demoted_reason: "day", demoted_until: "2026-11-20T00:00:00Z", resets_at: "2026-11-20T00:00:00Z",
          } }],
        },
      },
    };
    renderApp("/salute-sistema");
    const pq = await screen.findByRole("region", { name: "Qualità dei prezzi" });
    expect(within(pq).getByText("NVDA (da 16 min)")).toBeInTheDocument();
    expect(within(pq).getByText("NVDA 5")).toBeInTheDocument();
    expect(within(within(pq).getByRole("list", { name: "Classificazioni nelle 24 ore" })).getByText(/salto improvviso/)).toBeInTheDocument();
    const q = screen.getByRole("region", { name: "Quota delle fonti" });
    expect(within(q).getByRole("progressbar", { name: "Al giorno (UTC) twelvedata" })).toHaveAttribute("aria-valuenow", "95");
    expect(within(q).getByText(/quota giornaliera quasi esaurita/)).toBeInTheDocument();
  });
});
