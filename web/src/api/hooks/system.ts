import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDownload, apiFetch, saveBlob } from "@/api/client";
import { appSettingsKey } from "@/api/hooks/risk";
import type { AppSettings, BackupFile, HandoffWatchdog, PreopenCheck, SystemHealth, VerifyBalancesResult, VerifyChainResult, WeeklyGoal, WeeklyReportPdf } from "@/api/types";

export const systemHealthKey = ["system-health"] as const;

/** «Salute del sistema» (the api caches it 30 s): refreshed every minute. */
export function useSystemHealth() {
  return useQuery({
    queryKey: systemHealthKey,
    queryFn: () => apiFetch<SystemHealth>("/api/system-health"),
    refetchInterval: 60_000,
  });
}

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

export function useSaveMcpHideMode() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mcp_hide_mode: boolean) =>
      apiFetch<AppSettings>("/api/settings/app", { method: "PUT", body: { mcp_hide_mode } }),
    onSuccess: (data) => qc.setQueryData(appSettingsKey, data),
  });
}

/** The Telegram extras: weekly goal, pre-open check, hand-off watchdog and weekly PDF
 * report (partial updates). */
export function useSaveTelegramExtras() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      weekly_goal?: Partial<WeeklyGoal>;
      preopen_check?: Partial<PreopenCheck>;
      handoff_watchdog?: Partial<HandoffWatchdog>;
      weekly_report_pdf?: Partial<WeeklyReportPdf>;
      system_sentinel?: { on?: boolean };
    }) =>
      apiFetch<AppSettings>("/api/settings/app", { method: "PUT", body }),
    onSuccess: (data) => {
      qc.setQueryData(appSettingsKey, data);
      // the weekly goal is the «settimanale» goal: the goals page, dashboard and chart follow
      void qc.invalidateQueries({ queryKey: ["goals"] });
      void qc.invalidateQueries({ queryKey: ["equity-history"] });
    },
  });
}

/** POST /api/system/backup: a fresh pg_dump, saved as a file (429 while another runs). */
export function useDownloadBackup() {
  return useMutation({
    mutationFn: () => apiDownload("/api/system/backup", "smtrading.dump", { method: "POST" }),
    onSuccess: saveBlob,
  });
}
