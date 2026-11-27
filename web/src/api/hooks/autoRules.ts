import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  AutoBacktestBody,
  AutoRule,
  AutoRuleBody,
  AutoRuleCreate,
  AutoRuleResult,
  AutoRuleResultsParams,
  AutoTrade,
  ModelSpec,
  ReplayResult,
} from "@/api/types";

export const autoRulesKey = ["auto-rules"] as const;
/** the rules, results and trades refresh every 30 s while the tab is visible */
export const AUTO_RULES_REFRESH_MS = 30_000;

export type AutoRuleActionName = "activate" | "pause" | "resume";

/** The models and the bounds of each parameter (they do not change while the page is open). */
export function useAutoRuleModels() {
  return useQuery({
    queryKey: [...autoRulesKey, "models"],
    queryFn: () => apiFetch<ModelSpec[]>("/api/auto-rules/models"),
    staleTime: Infinity,
  });
}

/** The rules with their current version, of one portfolio or of all. */
export function useAutoRules(portfolioId?: string) {
  return useQuery({
    queryKey: [...autoRulesKey, "list", portfolioId ?? "all"],
    queryFn: () => apiFetch<AutoRule[]>(portfolioId ? `/api/auto-rules?portfolio_id=${encodeURIComponent(portfolioId)}` : "/api/auto-rules"),
    refetchInterval: AUTO_RULES_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

function useInvalidateRules() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: autoRulesKey });
}

export function useCreateAutoRule() {
  const invalidate = useInvalidateRules();
  return useMutation({
    mutationFn: (body: AutoRuleCreate) => apiFetch<AutoRule>("/api/auto-rules", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

/** A new version of a rule that is not active. */
export function useUpdateAutoRule(id: string) {
  const invalidate = useInvalidateRules();
  return useMutation({
    mutationFn: (body: AutoRuleBody) => apiFetch<AutoRule>(`/api/auto-rules/${id}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useAutoRuleAction(id: string, action: AutoRuleActionName) {
  const invalidate = useInvalidateRules();
  return useMutation({
    mutationFn: () => apiFetch<AutoRule>(`/api/auto-rules/${id}/${action}`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

/** The latest trades of a rule, newest first. */
export function useAutoRuleTrades(id: string | null, limit = 100) {
  return useQuery({
    queryKey: [...autoRulesKey, "trades", id, limit],
    queryFn: () => apiFetch<AutoTrade[]>(`/api/auto-rules/${id}/trades?limit=${limit}`),
    enabled: id !== null,
    refetchInterval: AUTO_RULES_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** Per rule and per UTC day. */
export function useAutoRuleResults(params: AutoRuleResultsParams = {}) {
  const q = new URLSearchParams();
  if (params.portfolio_id) q.set("portfolio_id", params.portfolio_id);
  if (params.from) q.set("from", params.from);
  if (params.to) q.set("to", params.to);
  const qs = q.toString();
  return useQuery({
    queryKey: [...autoRulesKey, "results", params.portfolio_id ?? "all", params.from ?? "", params.to ?? ""],
    queryFn: () => apiFetch<AutoRuleResult[]>(`/api/auto-rules/results${qs ? `?${qs}` : ""}`),
    refetchInterval: AUTO_RULES_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** The replay on one-minute history (one at a time on the server: a second one gets 429). */
export function useAutoRuleBacktest() {
  return useMutation({
    mutationFn: (body: AutoBacktestBody) => apiFetch<ReplayResult>("/api/auto-rules/backtest", { method: "POST", body }),
  });
}

/** End-of-drill cleanup of a test portfolio: deletes its rules and trades (admin). */
export function usePurgeAutoRules(portfolioId: string) {
  const invalidate = useInvalidateRules();
  return useMutation({
    mutationFn: () => apiFetch<Record<string, unknown>>(`/api/portfolios/${portfolioId}/auto-rules`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}
