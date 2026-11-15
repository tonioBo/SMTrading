import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { ActivityCatalog, ActivityFilters, ActivityRow } from "@/api/types";
import { zonedDayEnd, zonedDayStart } from "@/lib/zonedDate";

export const activityKey = ["activity-log"] as const;
export const ACTIVITY_PAGE = 50;
const TIME_ZONE = "Europe/Rome";

/** The filters as query parameters; the period's days are whole days in Europe/Rome. */
export function activityQuery(filters: ActivityFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.email) params.set("email", filters.email);
  if (filters.category) params.set("category", filters.category);
  if (filters.outcome) params.set("outcome", filters.outcome);
  if (filters.from) params.set("from", zonedDayStart(filters.from, TIME_ZONE));
  if (filters.to) params.set("to", zonedDayEnd(filters.to, TIME_ZONE));
  return params;
}

export function activityCsvUrl(filters: ActivityFilters): string {
  const qs = activityQuery(filters).toString();
  return `/api/activity-log.csv${qs ? `?${qs}` : ""}`;
}

/** The activity log (admins only), newest first; the next page goes before the last row's id. */
export function useActivityLog(filters: ActivityFilters, enabled = true) {
  return useInfiniteQuery({
    queryKey: [...activityKey, filters],
    initialPageParam: null as number | null,
    enabled,
    queryFn: ({ pageParam }) => {
      const params = activityQuery(filters);
      params.set("limit", String(ACTIVITY_PAGE));
      if (pageParam != null) params.set("before_id", String(pageParam));
      return apiFetch<ActivityRow[]>(`/api/activity-log?${params.toString()}`);
    },
    getNextPageParam: (last) => (last.length < ACTIVITY_PAGE ? null : last[last.length - 1].id),
  });
}

export function useActivityCatalog(enabled = true) {
  return useQuery({
    queryKey: [...activityKey, "catalog"],
    queryFn: () => apiFetch<ActivityCatalog>("/api/activity-log/catalog"),
    staleTime: Infinity,
    enabled,
  });
}
