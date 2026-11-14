import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AgentDocDetail, AgentDocInput, AgentDocVersionInfo } from "@/api/types";
import { renderMarkdown } from "@/lib/markdown";
import { fullInstructions } from "@/lib/agentDocs";
import { commonHandlers } from "@/test/handlers";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const TITLES: Record<string, string> = {
  readme: "Guida all'installazione",
  common: "Regole comuni",
  ceo: "CEO SMTrading",
  "portfolio-manager": "Portfolio Manager",
  "risk-manager": "Risk Manager",
  "execution-supervisor": "Execution Supervisor",
  "market-analyst": "Market Analyst",
  "news-analyst": "News Analyst",
  "quant-researcher": "Quant Researcher",
  "performance-auditor": "Performance Auditor",
  notifier: "Notifier",
};

const COMMON = "# 00 · Regole comuni\n\nRegola 1.\n\n";
const CEO = "\n# 01 · CEO SMTrading\n\n| Tool | Uso |\n|---|---|\n| `set_trading_halt` | halt |\n\nCursore feed=<id> <script>alert(1)</script>\n";

let docs: Record<string, AgentDocDetail>;
let puts: AgentDocInput[];
let restores: string[];
let conflictNext: boolean;
let exports: number;
let exportStatus: Record<string, unknown>;

function reset() {
  docs = Object.fromEntries(
    Object.entries(TITLES).map(([slug, title]) => [
      slug,
      {
        slug,
        title,
        current_version: 1,
        updated_at: "2026-11-14T08:00:00Z",
        updated_by: "seed",
        body: slug === "common" ? COMMON : slug === "ceo" ? CEO : `# ${title}\n\nTesto di ${title}.`,
      },
    ]),
  );
  puts = [];
  restores = [];
  conflictNext = false;
  exports = 0;
  exportStatus = {
    last_export_at: "2026-11-14T08:05:00Z",
    ok: true,
    error: null,
    dir: "/agent-instructions",
    entry_file: "AGENTS.md",
    agents: Object.keys(TITLES)
      .filter((s) => s !== "readme" && s !== "common")
      .map((slug) => ({ slug, path: `/smtrading-instructions/${slug}` })),
  };
}
reset();

const versions = (slug: string): AgentDocVersionInfo[] =>
  Array.from({ length: docs[slug].current_version }, (_, i) => docs[slug].current_version - i).map((v) => ({
    version: v,
    author: v === 1 ? "seed" : "admin@example.com",
    note: v === 1 ? null : "più breve",
    created_at: "2026-11-14T09:00:00Z",
    size: 120,
  }));

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agent-docs", () =>
    // the server's order is alphabetical: the page shows the roster order
    HttpResponse.json(
      Object.values(docs)
        .sort((a, b) => a.slug.localeCompare(b.slug))
        .map((d): Omit<AgentDocDetail, "body"> => ({
          slug: d.slug,
          title: d.title,
          current_version: d.current_version,
          updated_at: d.updated_at,
          updated_by: d.updated_by,
        })),
    ),
  ),
  http.get("*/api/agent-docs/export-status", () => HttpResponse.json(exportStatus)),
  http.post("*/api/agent-docs/export", () => {
    exports += 1;
    return HttpResponse.json({ exported: 9, dir: "/agent-instructions", ok: true });
  }),
  http.get("*/api/agent-docs/:slug", ({ params }) => HttpResponse.json(docs[params.slug as string])),
  http.get("*/api/agent-docs/:slug/versions", ({ params }) => HttpResponse.json(versions(params.slug as string))),
  http.get("*/api/agent-docs/:slug/versions/:v", ({ params }) =>
    HttpResponse.json({
      slug: params.slug,
      version: Number(params.v),
      body: `# Vecchio testo v${params.v}`,
      author: "seed",
      note: null,
      created_at: "2026-11-14T08:00:00Z",
    }),
  ),
  http.put("*/api/agent-docs/:slug", async ({ params, request }) => {
    const input = (await request.json()) as AgentDocInput;
    puts.push(input);
    const doc = docs[params.slug as string];
    if (conflictNext) {
      conflictNext = false;
      docs[doc.slug] = { ...doc, current_version: doc.current_version + 1, body: "# Testo di un altro" };
      return HttpResponse.json(
        {
          code: "VERSION_CONFLICT",
          message: "Il testo è stato modificato da qualcun altro, ricarica",
          details: { current_version: doc.current_version + 1 },
        },
        { status: 409 },
      );
    }
    docs[doc.slug] = { ...doc, body: input.body, current_version: doc.current_version + 1, updated_by: "admin@example.com" };
    return HttpResponse.json({ doc: docs[doc.slug], changed: true });
  }),
  http.post("*/api/agent-docs/:slug/restore/:v", ({ params }) => {
    restores.push(`${params.slug}@${params.v}`);
    const doc = docs[params.slug as string];
    docs[doc.slug] = { ...doc, body: `# Vecchio testo v${params.v}`, current_version: doc.current_version + 1 };
    return HttpResponse.json({ doc: docs[doc.slug] });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  reset();
});
afterAll(() => server.close());

function stubClipboard(writeText: (text: string) => Promise<void>) {
  const spy = vi.fn(writeText);
  Object.defineProperty(navigator, "clipboard", { value: { writeText: spy }, configurable: true });
  return spy;
}

const roster = () => screen.findByRole("navigation", { name: "Documenti" });

describe("fullInstructions", () => {
  it("puts the common rules first, then a rule, then the role", () => {
    expect(fullInstructions(COMMON, CEO)).toBe(`${COMMON.trim()}\n\n---\n\n${CEO.trim()}\n`);
  });
});

describe("renderMarkdown", () => {
  it("renders tables and code, shows raw HTML as text", () => {
    const html = renderMarkdown(CEO);
    expect(html).toContain('<div class="md-table"><table>');
    expect(html).toContain("<code>set_trading_halt</code>");
    expect(html).not.toContain("<script");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&lt;id&gt;");
  });
});

describe("AgentDocs page", () => {
  it("lists setup documents and agents with tags, in roster order; shows the guide first", async () => {
    renderApp("/istruzioni-agenti");
    const nav = await roster();
    const buttons = within(nav).getAllByRole("button");
    expect(buttons.map((b) => b.querySelector("span")?.textContent)).toEqual([
      "Guida all'installazione",
      "00 · Regole comuni",
      "CEO SMTrading",
      "Portfolio Manager",
      "Risk Manager",
      "Execution Supervisor",
      "Market Analyst",
      "News Analyst",
      "Quant Researcher",
      "Performance Auditor",
      "Notifier",
    ]);
    expect(within(buttons[5]).getByText("trade")).toBeInTheDocument();
    expect(within(buttons[3]).getByText("ogni ora")).toBeInTheDocument();
    expect(buttons[0]).toHaveAttribute("aria-current", "true");
    expect(await screen.findByText("Testo di Guida all'installazione.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copia testo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copia solo ruolo" })).not.toBeInTheDocument();
  });

  it("copies the full instructions (common + role) and the role alone", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard(async () => {});
    renderApp("/istruzioni-agenti?doc=ceo");
    await screen.findByRole("heading", { name: "01 · CEO SMTrading" });
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText(/Cursore feed=<id>/)).toBeInTheDocument();

    const full = screen.getByRole("button", { name: "Copia istruzioni complete" });
    await waitFor(() => expect(full).toBeEnabled());
    await user.click(full);
    expect(writeText).toHaveBeenLastCalledWith(fullInstructions(COMMON, CEO));
    expect(await screen.findByText("Istruzioni complete copiato negli appunti")).toHaveAttribute("role", "status");

    await user.click(screen.getByRole("button", { name: "Copia solo ruolo" }));
    expect(writeText).toHaveBeenLastCalledWith(CEO);
  });

  it("falls back to a selected textarea when the clipboard is refused", async () => {
    const user = userEvent.setup();
    stubClipboard(async () => {
      throw new Error("denied");
    });
    renderApp("/istruzioni-agenti?doc=notifier");
    await user.click(await screen.findByRole("button", { name: "Copia solo ruolo" }));
    const box = await screen.findByRole("textbox", { name: "Testo da copiare" });
    expect(box).toHaveValue(docs.notifier.body);
    expect(box).toHaveFocus();
    expect(screen.getByText(/premi Ctrl\+C/)).toHaveAttribute("role", "status");
  });

  it("edits with preview and saves a new version from the base version", async () => {
    const user = userEvent.setup();
    renderApp("/istruzioni-agenti?doc=risk-manager");
    await user.click(await screen.findByRole("button", { name: "Modifica" }));
    const text = screen.getByRole("textbox", { name: "Testo (Markdown)" });
    await user.clear(text);
    await user.type(text, "# Nuovo RM{enter}{enter}Soglia 5%.");
    await user.click(screen.getByRole("button", { name: "Anteprima" }));
    expect(within(screen.getByLabelText("Anteprima")).getByRole("heading", { name: "Nuovo RM" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Torna al testo" }));
    await user.type(screen.getByRole("textbox", { name: "Nota (facoltativa)" }), "soglia");
    await user.click(screen.getByRole("button", { name: "Salva" }));

    await waitFor(() => expect(puts).toEqual([{ body: "# Nuovo RM\n\nSoglia 5%.", note: "soglia", base_version: 1 }]));
    expect(await screen.findByText(/Versione 2/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Testo (Markdown)" })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Nuovo RM" })).toBeInTheDocument();
  });

  it("shows the conflict message and reloads the text saved by someone else", async () => {
    const user = userEvent.setup();
    conflictNext = true;
    renderApp("/istruzioni-agenti?doc=ceo");
    await user.click(await screen.findByRole("button", { name: "Modifica" }));
    await user.type(screen.getByRole("textbox", { name: "Testo (Markdown)" }), "aggiunta");
    await user.click(screen.getByRole("button", { name: "Salva" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Il testo è stato modificato da qualcun altro, ricarica");

    await user.click(within(alert).getByRole("button", { name: "Ricarica" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Testo (Markdown)" })).toHaveValue("# Testo di un altro"));
    expect(screen.getByText("Stai modificando la versione 2.")).toBeInTheDocument();
  });

  it("lists versions, shows an old one and restores it", async () => {
    const user = userEvent.setup();
    docs.ceo = { ...docs.ceo, current_version: 2 };
    renderApp("/istruzioni-agenti?doc=ceo");
    await user.click(await screen.findByRole("button", { name: "Versioni" }));
    const list = await screen.findByRole("region", { name: "Versioni" });
    expect(await within(list).findByText("più breve")).toBeInTheDocument();
    expect(within(list).getByText("testo iniziale")).toBeInTheDocument();
    expect(within(list).queryByRole("button", { name: "Ripristina versione 2" })).not.toBeInTheDocument();

    await user.click(within(list).getByRole("button", { name: "Visualizza versione 1" }));
    expect(await within(list).findByRole("heading", { name: "Vecchio testo v1" })).toBeInTheDocument();

    await user.click(within(list).getByRole("button", { name: "Ripristina versione 1" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Ripristina" }));
    await waitFor(() => expect(restores).toEqual(["ceo@1"]));
    expect(await screen.findByText(/Versione 3/)).toBeInTheDocument();
  });
});

describe("export for Paperclip", () => {
  it("shows the status line, runs «Esporta ora» and shows the External settings with copy", async () => {
    stubClipboard(async () => {});
    renderApp("/istruzioni-agenti?doc=risk-manager");
    expect(await screen.findByText(/Esportazione per Paperclip: aggiornata alle \d{2}:\d{2}/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Esporta ora" }));
    await waitFor(() => expect(exports).toBe(1));
    expect(await screen.findByText("/smtrading-instructions/risk-manager")).toBeInTheDocument();
    expect(screen.getByText("AGENTS.md")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copia Root path" }));
    expect(await screen.findByText("Root path copiato negli appunti")).toBeInTheDocument();
  });

  it("shows the error in Italian when the export failed", async () => {
    exportStatus = { ...exportStatus, ok: false, error: "Esportazione non riuscita: la cartella non esiste" };
    renderApp("/istruzioni-agenti?doc=ceo");
    expect(await screen.findByText(/non riuscita: Esportazione non riuscita: la cartella non esiste/)).toBeInTheDocument();
  });
});

