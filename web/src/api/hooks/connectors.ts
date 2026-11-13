import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { Connector, ConnectorPatch, ConnectorTestResult, ProviderPriority, StreamStatus } from "@/api/types";

export const connectorsKey = ["connectors"] as const;
export const prioritiesKey = ["provider-priorities"] as const;
export const streamStatusKey = ["stream-status"] as const;

export const STREAM_POLL_MS = 5000;

export function useConnectors() {
  return useQuery({ queryKey: connectorsKey, queryFn: () => apiFetch<Connector[]>("/api/connectors") });
}

export function usePatchConnector() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, patch }: { name: string; patch: ConnectorPatch }) =>
      apiFetch<Connector>(`/api/connectors/${name}`, { method: "PATCH", body: patch }),
    onSuccess: (data) =>
      qc.setQueryData<Connector[]>(connectorsKey, (old) => old?.map((c) => (c.name === data.name ? data : c))),
  });
}

export function useTestConnector() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => apiFetch<ConnectorTestResult>(`/api/connectors/${name}/test`, { method: "POST" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: connectorsKey }),
  });
}

export function usePriorities() {
  return useQuery({ queryKey: prioritiesKey, queryFn: () => apiFetch<ProviderPriority[]>("/api/provider-priorities") });
}

export function useSavePriority() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: ProviderPriority) => apiFetch<ProviderPriority>("/api/provider-priorities", { method: "PUT", body: p }),
    onSuccess: (data) =>
      qc.setQueryData<ProviderPriority[]>(prioritiesKey, (old) =>
        old?.map((p) => (p.asset_class === data.asset_class && p.capability === data.capability ? data : p)),
      ),
  });
}

export function useStreamStatus() {
  return useQuery({
    queryKey: streamStatusKey,
    queryFn: () => apiFetch<StreamStatus>("/api/marketdata/stream-status"),
    refetchInterval: STREAM_POLL_MS,
  });
}
