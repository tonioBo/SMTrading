import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { KillSwitchState } from "@/api/types";
import { useLiveMessage } from "@/lib/ws";

export const killSwitchKey = ["kill-switch"] as const;

/** Kill-switch state, kept fresh by the `kill_switch` live event. */
export function useKillSwitch() {
  const qc = useQueryClient();
  useLiveMessage("event", (event) => {
    if (event.type === "kill_switch" && typeof event.on === "boolean") {
      qc.setQueryData<KillSwitchState>(killSwitchKey, { on: event.on });
    }
  });
  return useQuery({ queryKey: killSwitchKey, queryFn: () => apiFetch<KillSwitchState>("/api/kill-switch") });
}
