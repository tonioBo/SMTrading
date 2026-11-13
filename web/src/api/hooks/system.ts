import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { appSettingsKey } from "@/api/hooks/risk";
import type { AppSettings, BackupFile, VerifyBalancesResult, VerifyChainResult } from "@/api/types";

export function useBackups() {
  return useQuery({ queryKey: ["backups"], queryFn: () => apiFetch<BackupFile[]>("/api/system/backups") });
}

/** Runs the chain and balance checks together. */
export function useVerifyIntegrity() {
  return useMutation({
    mutationFn: async () => {
      const [chain, balances] = await Promise.all([
        apiFetch<VerifyChainResult>("/api/system/verify-chain"),
        apiFetch<VerifyBalancesResult>("/api/system/verify-balances"),
      ]);
      return { chain, balances };
    },
  });
}

export function useSaveTimezone() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (timezone: string) => apiFetch<AppSettings>("/api/settings/app", { method: "PUT", body: { timezone } }),
    onSuccess: (data) => qc.setQueryData(appSettingsKey, data),
  });
}
