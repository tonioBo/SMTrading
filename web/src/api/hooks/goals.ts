import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { Goal, GoalCreate, GoalPatch, GoalProgress, GoalTemplate, PlanState } from "@/api/types";

export const goalsKey = ["goals"] as const;
export const goalsProgressKey = ["goals", "progress"] as const;
export const GOALS_REFRESH_MS = 60_000;

/** `GET /api/goals`: every goal, the active ones first. */
export function useGoals() {
  return useQuery({ queryKey: [...goalsKey, "list"], queryFn: () => apiFetch<Goal[]>("/api/goals") });
}

/** `GET /api/goals/progress`: the current period of every active goal. */
export function useGoalsProgress() {
  return useQuery({
    queryKey: goalsProgressKey,
    queryFn: () => apiFetch<GoalProgress[]>("/api/goals/progress"),
    refetchInterval: GOALS_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** `GET /api/goals/plans`: the active piani's latest ended period (closed, still being closed, …). */
export function useGoalPlans() {
  return useQuery({
    queryKey: [...goalsKey, "plans"],
    queryFn: () => apiFetch<PlanState[]>("/api/goals/plans"),
    refetchInterval: GOALS_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** `GET /api/goals/templates` (admin): ready goals, one per active portfolio when per
 * portfolio; fetched only when `enabled` (the «Modelli» list is open). */
export function useGoalTemplates(enabled: boolean) {
  return useQuery({
    queryKey: [...goalsKey, "templates"],
    queryFn: () => apiFetch<GoalTemplate[]>("/api/goals/templates"),
    enabled,
  });
}

function useInvalidateGoals() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: goalsKey });
    void qc.invalidateQueries({ queryKey: ["equity-history"] });
  };
}

export function useCreateGoal() {
  const invalidate = useInvalidateGoals();
  return useMutation({
    mutationFn: (body: GoalCreate) => apiFetch<Goal>("/api/goals", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function usePatchGoal() {
  const invalidate = useInvalidateGoals();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: GoalPatch }) =>
      apiFetch<Goal>(`/api/goals/${id}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

/** Soft delete: the goal stays listed as inactive. */
export function useDeactivateGoal() {
  const invalidate = useInvalidateGoals();
  return useMutation({
    mutationFn: (id: string) => apiFetch<Goal>(`/api/goals/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}
