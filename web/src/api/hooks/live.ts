import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { brokerLinksKey } from "@/api/hooks/brokers";
import { dashboardKey } from "@/api/hooks/dashboard";
import { portfoliosKey } from "@/api/hooks/portfolios";
import type {
  LiveArmInput,
  LiveAuditFilters,
  LiveAuditRow,
  LiveDisarmResult,
  LiveKeysInput,
  LiveLink,
  LiveStatus,
  LiveTestResult,
  Portfolio,
} from "@/api/types";

export const liveLinksKey = ["live-links"] as const;
export const liveStatusKey = ["live", "status"] as const;
export const liveAuditKey = ["live-audit"] as const;
export const LIVE_STATUS_MS = 30_000;
export const LIVE_AUDIT_PAGE = 50;

/** Everything a real-money action can change: links, status banner, portfolios, dashboard. */
function useInvalidateLive() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: liveLinksKey });
    void qc.invalidateQueries({ queryKey: liveStatusKey });
    void qc.invalidateQueries({ queryKey: liveAuditKey });
    void qc.invalidateQueries({ queryKey: brokerLinksKey, exact: true });
    void qc.invalidateQueries({ queryKey: portfoliosKey });
    void qc.invalidateQueries({ queryKey: dashboardKey });
  };
}

export function useLiveLinks() {
  return useQuery({ queryKey: liveLinksKey, queryFn: () => apiFetch<LiveLink[]>("/api/live-links") });
}

/** For the red banner and the fixed Telegram warning (every page). */
export function useLiveStatus() {
  return useQuery({
    queryKey: liveStatusKey,
    queryFn: () => apiFetch<LiveStatus>("/api/live/status"),
    staleTime: LIVE_STATUS_MS,
    refetchInterval: LIVE_STATUS_MS,
  });
}

export function useCreateLiveLink() {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (body: LiveKeysInput) => apiFetch<LiveLink>("/api/live-links", { method: "POST", body }),
    gcTime: 0, // the variables hold the keys: never kept in the mutation cache
    onSuccess: invalidate,
  });
}

export function useReplaceLiveKeys(linkId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (body: LiveKeysInput) => apiFetch<LiveLink>(`/api/live-links/${linkId}/keys`, { method: "PUT", body }),
    gcTime: 0, // the variables hold the keys
    onSuccess: invalidate,
  });
}

export function useTestLiveLink(linkId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: () => apiFetch<LiveTestResult>(`/api/live-links/${linkId}/test`, { method: "POST" }),
    onSettled: invalidate,
  });
}

export function useSetLiveDryRun(linkId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (on: boolean) => apiFetch<LiveLink>(`/api/live-links/${linkId}/dry-run`, { method: "POST", body: { on } }),
    onSuccess: invalidate,
  });
}

export function useArmLiveLink(linkId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (body: LiveArmInput) => apiFetch<LiveLink>(`/api/live-links/${linkId}/arm`, { method: "POST", body }),
    gcTime: 0, // the TOTP code
    onSettled: invalidate,
  });
}

/** «Disattiva denaro reale»: one click, no TOTP. */
export function useDisarmLiveLink(linkId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: () => apiFetch<LiveDisarmResult>(`/api/live-links/${linkId}/disarm`, { method: "POST", body: {} }),
    onSettled: invalidate,
  });
}

/** Link a portfolio to a real-money link (TOTP) or, with `linkId` null, unlink it. */
export function useSetLivePortfolio() {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: ({ portfolioId, linkId, totp }: { portfolioId: string; linkId: string | null; totp?: string }) =>
      apiFetch<Portfolio>(`/api/portfolios/${portfolioId}/live-link`, {
        method: "PUT",
        body: linkId ? { link_id: linkId, totp } : { link_id: null },
      }),
    gcTime: 0,
    onSuccess: invalidate,
  });
}

/** «Ho verificato, riprendi» (TOTP). */
export function useLiveResume(portfolioId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (totp: string) => apiFetch<Portfolio>(`/api/portfolios/${portfolioId}/live-resume`, { method: "POST", body: { totp } }),
    gcTime: 0,
    onSuccess: invalidate,
  });
}

/** «Chiudi tutte le posizioni reali» (TOTP): the ids of the orders placed. */
export function useLiveFlatten(portfolioId: string) {
  const invalidate = useInvalidateLive();
  return useMutation({
    mutationFn: (totp: string) =>
      apiFetch<{ orders: string[] }>(`/api/portfolios/${portfolioId}/live-flatten`, { method: "POST", body: { totp } }),
    gcTime: 0,
    onSuccess: invalidate,
  });
}

export function liveAuditQuery(filters: LiveAuditFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.link_id) params.set("link_id", filters.link_id);
  if (filters.portfolio_id) params.set("portfolio_id", filters.portfolio_id);
  if (filters.action) params.set("action", filters.action);
  return params;
}

export function liveAuditCsvUrl(filters: LiveAuditFilters): string {
  const qs = liveAuditQuery(filters).toString();
  return `/api/live-audit.csv${qs ? `?${qs}` : ""}`;
}

/** The live audit, newest first; the next page goes before the last row's id. */
export function useLiveAudit(filters: LiveAuditFilters) {
  return useInfiniteQuery({
    queryKey: [...liveAuditKey, filters],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = liveAuditQuery(filters);
      params.set("limit", String(LIVE_AUDIT_PAGE));
      if (pageParam != null) params.set("before_id", String(pageParam));
      return apiFetch<LiveAuditRow[]>(`/api/live-audit?${params.toString()}`);
    },
    getNextPageParam: (last) => (last.length < LIVE_AUDIT_PAGE ? null : last[last.length - 1].id),
  });
}
