import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { TvAdminItem, TvArea, TvDiscovered, TvDisplayPatch, TvPower, TvSendResult } from "@/api/tvTypes";

/** «Schermo TV»: the user's own area (their TVs, display link, selection, sounds). */
export const tvKey = ["tv"] as const;
export const tvAdminKey = ["tv", "admin"] as const;
/** while a pairing waits for the answer on the TV, the area is read this often */
export const TV_PAIRING_POLL_MS = 1_500;
/** how long the TV shows its prompt (the backend waits up to 30 s) */
export const TV_PAIRING_S = 30;
export const TV_POWER_POLL_MS = 30_000;

export function useTvArea() {
  return useQuery({
    queryKey: tvKey,
    queryFn: () => apiFetch<TvArea>("/api/tv"),
    refetchInterval: (q) => (q.state.data?.tvs.some((t) => t.pairing.state === "waiting") ? TV_PAIRING_POLL_MS : false),
  });
}

export function useTvPower(id: string, enabled: boolean) {
  return useQuery({
    queryKey: ["tv", "power", id],
    queryFn: () => apiFetch<{ power: TvPower }>(`/api/tv/screens/${encodeURIComponent(id)}/power`),
    enabled,
    refetchInterval: TV_POWER_POLL_MS,
    retry: false,
  });
}

/** a write that answers with the whole area */
function useAreaWrite<V>(fn: (v: V) => Promise<TvArea>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (area) => {
      qc.setQueryData(tvKey, area);
      void qc.invalidateQueries({ queryKey: tvAdminKey });
    },
  });
}

export const useAddTv = () =>
  useAreaWrite((body: { host: string; name?: string }) => apiFetch<TvArea>("/api/tv/screens", { method: "POST", body }));

export const useRemoveTv = () =>
  useAreaWrite((id: string) => apiFetch<TvArea>(`/api/tv/screens/${encodeURIComponent(id)}`, { method: "DELETE" }));

export const usePairTv = () =>
  useAreaWrite((id: string) => apiFetch<TvArea>(`/api/tv/screens/${encodeURIComponent(id)}/pair`, { method: "POST" }));

/** saves the selection, the portal address or the sounds; never contacts the TV */
export const useSaveTvDisplay = () => useAreaWrite((patch: TvDisplayPatch) => apiFetch<TvArea>("/api/tv/display", { method: "PUT", body: patch }));

export const useRotateTvLink = () => useAreaWrite<void>(() => apiFetch<TvArea>("/api/tv/link/rotate", { method: "POST" }));
export const useRevokeTvLink = () => useAreaWrite<void>(() => apiFetch<TvArea>("/api/tv/link/revoke", { method: "POST" }));

/** «Abbina con codice»: the code shown by a TV page opens the caller's own link there */
export function useClaimTvCode() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (code: string) => apiFetch<{ ok: boolean; message: string }>("/api/tv/claim", { method: "POST", body: { code } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: tvKey }),
  });
}

/** «Manda alla TV»: the only action that turns a TV on */
export function useSendToTv() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; screen?: string; rotation?: boolean; interval_s?: number }) =>
      apiFetch<TvSendResult>(`/api/tv/screens/${encodeURIComponent(id)}/send`, { method: "POST", body }),
    onSettled: () => void qc.invalidateQueries({ queryKey: tvKey }),
  });
}

export function usePowerOffTv() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<{ ok: boolean; message: string }>(`/api/tv/screens/${encodeURIComponent(id)}/power-off`, { method: "POST" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: tvKey }),
  });
}

/** admins: the TVs on the portal's network */
export function useDiscoverTvs() {
  return useMutation({
    mutationFn: (subnet: string | null) =>
      apiFetch<{ subnet: string; devices: TvDiscovered[] }>("/api/tv/discover", { method: "POST", body: subnet ? { subnet } : {} }),
  });
}

/** admins: every user's TVs */
export function useAllTvs(enabled: boolean) {
  return useQuery({
    queryKey: tvAdminKey,
    queryFn: () => apiFetch<{ items: TvAdminItem[] }>("/api/tv/admin/screens"),
    enabled,
  });
}

export function useAdminRevokeTv() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<TvAdminItem>(`/api/tv/admin/screens/${encodeURIComponent(id)}/revoke`, { method: "POST" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: tvAdminKey });
      void qc.invalidateQueries({ queryKey: tvKey });
    },
  });
}
