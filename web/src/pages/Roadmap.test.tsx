import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Roadmap as RoadmapData, RoadmapLive } from "@/api/hooks/roadmap";
import Roadmap from "./Roadmap";

const hooks = vi.hoisted(() => ({ useRoadmap: vi.fn() }));
vi.mock("@/api/hooks/roadmap", async (orig) => ({
  ...(await orig<typeof import("@/api/hooks/roadmap")>()),
  useRoadmap: hooks.useRoadmap,
}));

const LIVE: RoadmapLive = {
  campaign: { start: "2026-11-23", end: "2026-12-23", day: 6, total_days: 30, state: "in_corso" },
  equity: { current_eur: "30412.5", as_of: "2026-11-28T14:00:00Z", portfolios: 3 },
  goal: {
    label: "Mensile +5%",
    name: "mensile",
    status: "in linea",
    start_eur: "30000",
    current_eur: "30412.5",
    target_eur: "31500",
    pct: "1.38",
    target_pct: "5",
    period_start: "2026-11-01",
    period_end: "2026-11-30",
    as_of: "2026-11-28T14:00:00Z",
  },
  drills: { done: 3, planned: 10, next: { title: "Ripristino del backup e della chiave", date: "2026-11-29" } },
  go_no_go: { date: "2026-12-23", days_left: 25 },
  neurograph: { tools: ["get_forecast"], count: 1 },
  notes: [],
};

const DATA: RoadmapData = {
  updated: "2026-11-28",
  summary: "Siamo nella campagna di dicembre.",
  campaign: {
    name: "Campagna di dicembre",
    start: "2026-11-23",
    end: "2026-12-23",
    portfolios: [{ code: "AGG", name: "Portafoglio Alpaca", kind: "conto di prova Alpaca" }],
    goal: { label: "Mensile +5%", kind: "min", target_pct: "5", horizon: "month" },
  },
  go_no_go: "2026-12-23",
  stages: [
    {
      id: "fondamenta",
      title: "Le fondamenta",
      status: "fatto",
      owner: "smtrading",
      start: "2026-11-12",
      end: "2026-11-19",
      what: "SMTrading è nato da zero.",
      why: "Ogni euro resta registrato.",
      items: [{ title: "Piano 1 · Registro", status: "fatto", detail: "Registro a partita doppia.", refs: ["docs/superpowers/plans/p1.md"], date: "2026-11-12" }],
    },
    {
      id: "campagna-dicembre",
      title: "Campagna di dicembre",
      status: "in_corso",
      owner: "smtrading",
      start: "2026-11-23",
      end: "2026-12-23",
      what: "Un mese di lavoro vero.",
      why: "È la prova generale.",
      items: [
        { title: "Broker che non risponde", status: "fatto", detail: "Sospeso 10 minuti.", refs: [], date: "2026-11-27", kind: "esercitazione" },
        { title: "Ripristino del backup e della chiave", status: "prossimo", detail: "Database usa e getta.", refs: [], date: "2026-11-29", kind: "esercitazione" },
      ],
    },
    {
      id: "neurograph",
      title: "Neurograph: misure per gli agenti",
      status: "in_corso",
      owner: "neurograph",
      start: "2026-11-28",
      what: "Il laboratorio di Luca.",
      why: "Misure verificate.",
      items: [{ title: "Test T0", status: "fatto", detail: "GARCH ha battuto.", refs: [], owner: "neurograph", kind: "esperimento" }],
    },
  ],
  glossary: [
    { term: "MCP", meaning: "La porta degli agenti." },
    { term: "Paperclip", meaning: "La piattaforma degli agenti." },
  ],
  live: LIVE,
};

function ready(data: RoadmapData = DATA) {
  hooks.useRoadmap.mockReturnValue({ data, isLoading: false, isError: false });
}

beforeEach(() => hooks.useRoadmap.mockReset());

describe("Roadmap", () => {
  it("shows the summary and the live numbers", () => {
    ready();
    render(<Roadmap />);
    expect(screen.getByRole("heading", { level: 1, name: "Roadmap" })).toBeInTheDocument();
    expect(screen.getByText("Siamo nella campagna di dicembre.")).toBeInTheDocument();
    expect(screen.getByText(/Aggiornata il 28\/11\/2026/)).toBeInTheDocument();
    const live = screen.getByRole("region", { name: "Numeri dal vivo" });
    expect(within(live).getByText("Giorno 6 di 30")).toBeInTheDocument();
    expect(within(live).getByText("30.412,50 €")).toBeInTheDocument();
    expect(within(live).getByText(/traguardo 31\.500,00 €/)).toBeInTheDocument();
    expect(within(live).getByText(/\+1,38% su \+5,00%/)).toBeInTheDocument();
    expect(within(live).getByText("3 su 10")).toBeInTheDocument();
    expect(within(live).getByText(/prossima 29\/11/)).toBeInTheDocument();
    expect(within(live).getByText("tra 25 giorni")).toBeInTheDocument();
    expect(within(live).getByText("1 in uso")).toBeInTheDocument();
    expect(within(live).getByText("get_forecast")).toBeInTheDocument();
  });

  it("marks «sei qui» on the first stage in progress only", () => {
    ready();
    render(<Roadmap />);
    const here = screen.getAllByText("Sei qui");
    expect(here).toHaveLength(1);
    const stage = here[0].closest("li")!;
    expect(within(stage).getByRole("heading", { name: "Campagna di dicembre" })).toBeInTheDocument();
    expect(stage).toHaveAttribute("aria-current", "step");
  });

  it("opens a stage to show its items with status, date and references", async () => {
    const user = userEvent.setup();
    ready();
    render(<Roadmap />);
    expect(screen.queryByText("Registro a partita doppia.")).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /Dettagli: Le fondamenta/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = screen.getByRole("list", { name: "Voci di Le fondamenta" });
    expect(within(list).getByText("Piano 1 · Registro")).toBeInTheDocument();
    expect(within(list).getByText("Registro a partita doppia.")).toBeInTheDocument();
    expect(within(list).getByText("12/11")).toBeInTheDocument();
    expect(within(list).getByText("docs/superpowers/plans/p1.md")).toBeInTheDocument();
    expect(within(list).getByText("Fatto")).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.queryByText("Registro a partita doppia.")).not.toBeInTheDocument();
  });

  it("shows the stage texts, owners and dates", () => {
    ready();
    render(<Roadmap />);
    expect(screen.getByText("Un mese di lavoro vero.")).toBeInTheDocument();
    expect(screen.getByText("È la prova generale.")).toBeInTheDocument();
    expect(screen.getByText("Luca · Neurograph")).toBeInTheDocument();
    const campaign = screen.getByRole("heading", { name: "Campagna di dicembre" }).closest("li")!;
    expect(within(campaign).getByText("23/11 → 23/12")).toBeInTheDocument();
    expect(screen.getByText("dal 28/11")).toBeInTheDocument();
  });

  it("shows «—» and the note when the goal and the equity are missing", () => {
    ready({
      ...DATA,
      live: { ...LIVE, equity: null, goal: null, notes: ["Obiettivo «Mensile +5%» sul totale non impostato: numeri non disponibili."] },
    });
    render(<Roadmap />);
    const live = screen.getByRole("region", { name: "Numeri dal vivo" });
    expect(within(live).getAllByText("—").length).toBeGreaterThanOrEqual(1);
    expect(within(live).getByText(/sul totale non impostato/)).toBeInTheDocument();
  });

  it("lists the glossary", () => {
    ready();
    render(<Roadmap />);
    const glossary = screen.getByRole("region", { name: "Glossario" });
    expect(within(glossary).getByText("MCP")).toBeInTheDocument();
    expect(within(glossary).getByText("La porta degli agenti.")).toBeInTheDocument();
    expect(within(glossary).getByText("Paperclip")).toBeInTheDocument();
  });

  it("loading and error states", () => {
    hooks.useRoadmap.mockReturnValue({ data: undefined, isLoading: true, isError: false });
    const { unmount } = render(<Roadmap />);
    expect(screen.getByText("Caricamento…")).toBeInTheDocument();
    unmount();
    hooks.useRoadmap.mockReturnValue({ data: undefined, isLoading: false, isError: true });
    render(<Roadmap />);
    expect(screen.getByText("Impossibile caricare la roadmap")).toBeInTheDocument();
  });
});
