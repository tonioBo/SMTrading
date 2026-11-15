import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, resetUnauthorized } from "@/api/client";
import type { AdminUser, LoginRequest } from "@/api/types";

export const meKey = ["auth", "me"] as const;

export function useMe() {
  return useQuery({ queryKey: meKey, queryFn: () => apiFetch<AdminUser>("/api/auth/me") });
}

/** False only for a «Sola lettura» user: edit, create and delete controls are hidden
 * (the server refuses them anyway). The emergency actions stay available to everyone. */
export function useIsAdmin(): boolean {
  const { data } = useMe();
  return data?.role !== "viewer";
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: LoginRequest) => apiFetch<AdminUser>("/api/auth/login", { method: "POST", body }),
    onSuccess: (user) => {
      resetUnauthorized();
      qc.setQueryData(meKey, user);
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<void>("/api/auth/logout", { method: "POST" }),
    onSettled: () => qc.clear(),
  });
}

/** Changes the password; the server re-issues this session's cookies, so /me is refetched. */
export function useChangePassword() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { current: string; new: string }) =>
      apiFetch<{ ok: boolean }>("/api/auth/password", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: meKey }),
  });
}

export interface TotpSetup {
  secret: string;
  otpauth_uri: string;
}

/** A new (not yet active) TOTP secret; it becomes active with useTotpEnable. */
export function useTotpSetup() {
  return useMutation({
    gcTime: 0, // the secret does not linger in the mutation cache
    mutationFn: () => apiFetch<TotpSetup>("/api/auth/totp/setup", { method: "POST" }),
  });
}

/** Enables (or disables) TOTP; the server re-issues this session's cookies: /me is refetched. */
export function useTotpToggle(action: "enable" | "disable") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (code: string) => apiFetch<AdminUser>(`/api/auth/totp/${action}`, { method: "POST", body: { code } }),
    onSuccess: (user) => {
      qc.setQueryData(meKey, user);
      return qc.invalidateQueries({ queryKey: meKey });
    },
  });
}

export function useRevokeOtherSessions() {
  return useMutation({
    mutationFn: () => apiFetch<{ revoked: number }>("/api/auth/sessions/revoke-others", { method: "POST" }),
  });
}
