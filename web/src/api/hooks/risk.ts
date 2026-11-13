import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { AppSettings, InterestRate, RiskProfile, RiskProfileInput } from "@/api/types";

export const riskProfilesKey = ["risk-profiles"] as const;
export const interestRatesKey = ["interest-rates"] as const;
export const appSettingsKey = ["app-settings"] as const;

export function useSaveRiskProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, profile }: { id: string | null; profile: RiskProfileInput }) =>
      id
        ? apiFetch<RiskProfile>(`/api/risk-profiles/${id}`, { method: "PATCH", body: profile })
        : apiFetch<RiskProfile>("/api/risk-profiles", { method: "POST", body: profile }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: riskProfilesKey }),
  });
}

export function useInterestRates() {
  return useQuery({ queryKey: interestRatesKey, queryFn: () => apiFetch<InterestRate[]>("/api/interest-rates") });
}

/** Replaces the whole interest-rate table. */
export function useSaveInterestRates() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (rates: InterestRate[]) => apiFetch<InterestRate[]>("/api/interest-rates", { method: "PUT", body: rates }),
    onSuccess: (data) => qc.setQueryData(interestRatesKey, data),
  });
}

export function useAppSettings() {
  return useQuery({ queryKey: appSettingsKey, queryFn: () => apiFetch<AppSettings>("/api/settings/app") });
}

export function useSaveAppSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Pick<AppSettings, "stale_quote_s" | "default_spread_bps">) =>
      apiFetch<AppSettings>("/api/settings/app", { method: "PUT", body }),
    onSuccess: (data) => qc.setQueryData(appSettingsKey, data),
  });
}
