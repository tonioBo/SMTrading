import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, resetUnauthorized } from "@/api/client";
import type { AdminUser, LoginRequest } from "@/api/types";

export const meKey = ["auth", "me"] as const;

export function useMe() {
  return useQuery({ queryKey: meKey, queryFn: () => apiFetch<AdminUser>("/api/auth/me") });
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
