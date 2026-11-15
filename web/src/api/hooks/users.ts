import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { PortalUser, PortalUserWithPassword, UserRole } from "@/api/types";

export const usersKey = ["users"] as const;

export function useUsers(enabled = true) {
  return useQuery({ queryKey: usersKey, queryFn: () => apiFetch<PortalUser[]>("/api/users"), enabled });
}

function useUsersMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const qc = useQueryClient();
  return useMutation({
    gcTime: 0, // a temporary password does not linger in the mutation cache
    mutationFn: fn,
    onSettled: () => void qc.invalidateQueries({ queryKey: usersKey }),
  });
}

/** `totp`: the actor's code, needed to create an admin when the actor has TOTP. */
export function useCreateUser() {
  return useUsersMutation((body: { email: string; role: UserRole; totp?: string }) =>
    apiFetch<PortalUserWithPassword>("/api/users", { method: "POST", body }),
  );
}

export function useSetUserRole() {
  return useUsersMutation(({ id, ...body }: { id: string; role: UserRole; totp?: string }) =>
    apiFetch<{ user: PortalUser }>(`/api/users/${id}/role`, { method: "PUT", body }),
  );
}

export function useSetUserActive() {
  return useUsersMutation(({ id, active }: { id: string; active: boolean }) =>
    apiFetch<{ user: PortalUser }>(`/api/users/${id}/${active ? "enable" : "disable"}`, { method: "POST" }),
  );
}

export function useResetUserPassword() {
  return useUsersMutation(({ id, totp }: { id: string; totp?: string }) =>
    apiFetch<PortalUserWithPassword>(`/api/users/${id}/reset-password`, { method: "POST", body: totp ? { totp } : {} }),
  );
}

export function useResetUserTotp() {
  return useUsersMutation((id: string) => apiFetch<{ user: PortalUser }>(`/api/users/${id}/reset-totp`, { method: "POST" }));
}
