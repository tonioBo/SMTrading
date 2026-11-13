import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { dashboardKey } from "@/api/hooks/dashboard";
import { newKey, portfoliosKey } from "@/api/hooks/portfolios";
import type { BrokerLink, BrokerLinkCreate, BrokerLinkPatch, BrokerTestResult, Portfolio, Reconciliation } from "@/api/types";

export const brokerLinksKey = ["broker-links"] as const;
export const reconciliationsKey = (linkId: string) => ["broker-links", linkId, "reconciliations"] as const;
export const brokerPositionsKey = (linkId: string) => ["broker-links", linkId, "positions"] as const;

export const RECONCILIATION_LIMIT = 50;

export function useBrokerLinks() {
  return useQuery({ queryKey: brokerLinksKey, queryFn: () => apiFetch<BrokerLink[]>("/api/broker-links") });
}

function replaceLink(old: BrokerLink[] | undefined, link: BrokerLink) {
  return old?.map((l) => (l.id === link.id ? link : l));
}

export function useCreateBrokerLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, key }: { body: BrokerLinkCreate; key: string }) =>
      apiFetch<BrokerLink>("/api/broker-links", { method: "POST", body, idempotencyKey: key }),
    gcTime: 0, // the variables hold the keys: never kept in the mutation cache
    onSuccess: () => void qc.invalidateQueries({ queryKey: brokerLinksKey, exact: true }),
  });
}

export function usePatchBrokerLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: BrokerLinkPatch }) =>
      apiFetch<BrokerLink>(`/api/broker-links/${id}`, { method: "PATCH", body: patch }),
    gcTime: 0, // the variables may hold keys
    onSuccess: (link) => qc.setQueryData<BrokerLink[]>(brokerLinksKey, (old) => replaceLink(old, link)),
  });
}

export function useTestBrokerLink() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<BrokerTestResult>(`/api/broker-links/${id}/test`, { method: "POST", idempotencyKey: newKey() }),
    onSettled: () => void qc.invalidateQueries({ queryKey: brokerLinksKey, exact: true }),
  });
}

export function useSetPortfolioBroker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ portfolioId, linkId }: { portfolioId: string; linkId: string | null }) =>
      apiFetch<Portfolio>(`/api/portfolios/${portfolioId}/broker-link`, { method: "PUT", body: { broker_link_id: linkId } }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: brokerLinksKey, exact: true });
      void qc.invalidateQueries({ queryKey: portfoliosKey });
      void qc.invalidateQueries({ queryKey: dashboardKey }); // counts and the mode banner
    },
  });
}

export function useReconciliations(linkId: string) {
  return useQuery({
    queryKey: reconciliationsKey(linkId),
    queryFn: () => apiFetch<Reconciliation[]>(`/api/broker-links/${linkId}/reconciliations?limit=${RECONCILIATION_LIMIT}`),
  });
}

export function useReconcileNow(linkId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<{ reconciliations: Reconciliation[] }>(`/api/broker-links/${linkId}/reconcile`, {
        method: "POST",
        idempotencyKey: newKey(),
      }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: reconciliationsKey(linkId) });
      void qc.invalidateQueries({ queryKey: brokerLinksKey, exact: true });
    },
  });
}

/** Read on demand (network call to the broker). */
export function useBrokerPositions(linkId: string, enabled: boolean) {
  return useQuery({
    queryKey: brokerPositionsKey(linkId),
    queryFn: () => apiFetch<{ positions: { symbol: string; qty: string }[] }>(`/api/broker-links/${linkId}/positions`),
    enabled,
    retry: false,
  });
}
