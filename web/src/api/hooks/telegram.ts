import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  Decision,
  DecisionFilters,
  OutboxItem,
  TelegramChat,
  TelegramConfigPatch,
  TelegramSettings,
  TelegramTestResult,
} from "@/api/types";
import { useLiveMessage } from "@/lib/ws";

export const telegramKey = ["telegram"] as const;
export const outboxKey = ["telegram", "outbox"] as const;
export const decisionsKey = ["decisions"] as const;

/** the page refreshes the settings counters and the outbox this often (and on WS `notification`) */
export const TELEGRAM_POLL_MS = 15_000;
export const OUTBOX_LIMIT = 50;
export const DECISIONS_LIMIT = 50;

export function useTelegram() {
  return useQuery({
    queryKey: telegramKey,
    queryFn: () => apiFetch<TelegramSettings>("/api/telegram"),
    refetchInterval: TELEGRAM_POLL_MS,
  });
}

/** Saves the changed fields; the variables may hold the bot token, hence gcTime 0. */
export function useSaveTelegramConfig() {
  const qc = useQueryClient();
  return useMutation({
    gcTime: 0,
    mutationFn: (patch: TelegramConfigPatch) => apiFetch<TelegramSettings>("/api/telegram/config", { method: "PUT", body: patch }),
    onSuccess: (data) => qc.setQueryData(telegramKey, data),
  });
}

/** Groups seen by the bot; with `bot_token` the not-yet-saved token is used. */
export function useDetectChats() {
  return useMutation({
    gcTime: 0,
    mutationFn: (botToken: string | null) =>
      apiFetch<{ chats: TelegramChat[] }>("/api/telegram/detect-chats", {
        method: "POST",
        body: botToken ? { bot_token: botToken } : {},
      }),
  });
}

export function useSendTestMessage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<TelegramTestResult>("/api/telegram/test", { method: "POST" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: outboxKey }),
  });
}

export function useSetTelegramPaused() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (paused: boolean) => apiFetch<TelegramSettings>("/api/telegram/paused", { method: "PUT", body: { paused } }),
    onSuccess: (data) => qc.setQueryData(telegramKey, data),
    onSettled: () => void qc.invalidateQueries({ queryKey: outboxKey }),
  });
}

/** The outbox, newest first; refreshed every 15 s and on every WS `notification` event. */
export function useOutbox(status: string) {
  const qc = useQueryClient();
  useLiveMessage("event", (event) => {
    if (!event.type.startsWith("notification")) return;
    void qc.invalidateQueries({ queryKey: outboxKey });
    void qc.invalidateQueries({ queryKey: telegramKey, exact: true });
  });
  const params = new URLSearchParams({ limit: String(OUTBOX_LIMIT) });
  if (status) params.set("status", status);
  return useQuery({
    queryKey: [...outboxKey, status],
    queryFn: () => apiFetch<{ items: OutboxItem[] }>(`/api/telegram/outbox?${params.toString()}`),
    refetchInterval: TELEGRAM_POLL_MS,
  });
}

export function useRetryOutbox() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch<OutboxItem>(`/api/telegram/outbox/${id}/retry`, { method: "POST" }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: outboxKey });
      void qc.invalidateQueries({ queryKey: telegramKey, exact: true });
    },
  });
}

/** «Riprova tutti i falliti»: every failed message (tests excepted) goes back to the queue. */
export function useRetryAllFailed() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<{ retried: number }>("/api/telegram/outbox/retry-failed", { method: "POST" }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: outboxKey });
      void qc.invalidateQueries({ queryKey: telegramKey, exact: true });
    },
  });
}

/** Decisions log, newest first, paged with `before_id` («Carica altre»). */
export function useDecisions(filters: DecisionFilters) {
  return useInfiniteQuery({
    queryKey: [...decisionsKey, filters],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(DECISIONS_LIMIT) });
      if (filters.agent_id) params.set("agent_id", filters.agent_id);
      if (filters.portfolio_id) params.set("portfolio_id", filters.portfolio_id);
      if (filters.kind) params.set("kind", filters.kind);
      if (pageParam !== null) params.set("before_id", String(pageParam));
      return apiFetch<{ items: Decision[] }>(`/api/decisions?${params.toString()}`);
    },
    getNextPageParam: (last) =>
      last.items.length < DECISIONS_LIMIT ? undefined : last.items[last.items.length - 1].id,
  });
}
