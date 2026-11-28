import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

/** «Roadmap»: the curated file of SMTrading's road plus the live numbers
 * (`GET /api/roadmap`, every portal user). The text changes only with a release; the
 * numbers are read again every 5 minutes. */
export const ROADMAP_MS = 5 * 60_000;

export type RoadmapStatus = "fatto" | "in_corso" | "prossimo" | "futuro";
export type RoadmapOwner = "smtrading" | "neurograph" | "entrambi" | "matteo";
export type RoadmapKind = "lavoro" | "esercitazione" | "esperimento" | "strumento" | "criterio" | "prova";

export interface RoadmapItem {
  title: string;
  status: RoadmapStatus;
  detail: string;
  /** repository paths of the specs, plans and documents behind the item */
  refs: string[];
  owner?: RoadmapOwner;
  /** YYYY-MM-DD */
  date?: string;
  kind?: RoadmapKind;
}

export interface RoadmapStage {
  id: string;
  title: string;
  status: RoadmapStatus;
  owner: RoadmapOwner;
  start: string;
  end?: string | null;
  what: string;
  why: string;
  items: RoadmapItem[];
}

export interface RoadmapGoal {
  label: string;
  name: string;
  status: string;
  start_eur: string | null;
  current_eur: string | null;
  target_eur: string | null;
  pct: string | null;
  target_pct: string;
  period_start: string;
  period_end: string;
  as_of: string | null;
}

export interface RoadmapLive {
  campaign: { start: string; end: string; day: number; total_days: number; state: "prima" | "in_corso" | "conclusa" };
  /** the portfolios in the totals, latest valuation each (null: no snapshot yet) */
  equity: { current_eur: string; as_of: string; portfolios: number } | null;
  /** the campaign goal on the total (null: not set) */
  goal: RoadmapGoal | null;
  drills: { done: number; planned: number; next: { title: string; date: string } | null };
  go_no_go: { date: string; days_left: number };
  neurograph: { tools: string[]; count: number };
  /** short notes on the numbers that are missing */
  notes: string[];
}

export interface Roadmap {
  updated: string;
  summary: string;
  campaign: {
    name: string;
    start: string;
    end: string;
    portfolios: { code: string; name: string; kind: string }[];
    goal: { label: string; kind: string; target_pct: string; horizon: string };
  };
  go_no_go: string;
  stages: RoadmapStage[];
  glossary: { term: string; meaning: string }[];
  live: RoadmapLive;
}

export function useRoadmap() {
  return useQuery({
    queryKey: ["roadmap"],
    queryFn: () => apiFetch<Roadmap>("/api/roadmap"),
    staleTime: ROADMAP_MS,
    refetchInterval: ROADMAP_MS,
    refetchIntervalInBackground: false,
  });
}
