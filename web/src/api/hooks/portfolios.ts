import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { apiFetch } from "@/api/client";
import { dashboardKey } from "@/api/hooks/dashboard";
import type {
  CashMove,
  EquityPeriod,
  JournalType,
  Order,
  Performance,
  Portfolio,
  PortfolioCreate,
  PortfolioPatch,
  Position,
  ResetRequest,
  ResetResult,
  RiskProfile,
  Transaction,
  TransactionPage,
  Valuation,
} from "@/api/types";
import { useLiveMessage, useLiveSocket } from "@/lib/ws";
import { zonedDayEnd, zonedDayStart } from "@/lib/zonedDate";

export const portfoliosKey = ["portfolios"] as const;
const portfolioKey = (id: string) => ["portfolios", id] as const;

export const POSITIONS_REFRESH_MS = 10_000;
export const TX_PAGE_SIZE = 50;

/** Journal types an admin may reverse: executions are corrected with an opposite order. */
const NON_REVERSIBLE: ReadonlySet<JournalType> = new Set([
  "TRADE",
  "LIQUIDATION",
  "CORPORATE_ACTION",
  "REVERSAL",
]);

export function isReversible(tx: Transaction): boolean {
  return !NON_REVERSIBLE.has(tx.type) && tx.reversed_by == null;
}

/** Open (non-final) order statuses: these can be canceled. */
export const OPEN_ORDER_STATUSES: ReadonlySet<string> = new Set(["new", "held", "queued", "pending", "triggered"]);

export function usePortfolios() {
  return useQuery({ queryKey: portfoliosKey, queryFn: () => apiFetch<Portfolio[]>("/api/portfolios") });
}

export function usePortfolio(id: string) {
  return useQuery({ queryKey: portfolioKey(id), queryFn: () => apiFetch<Portfolio>(`/api/portfolios/${id}`) });
}

export function useValuation(id: string) {
  return useQuery({
    queryKey: [...portfolioKey(id), "valuation"],
    queryFn: () => apiFetch<Valuation>(`/api/portfolios/${id}/valuation`),
  });
}

export function usePositions(id: string) {
  return useQuery({
    queryKey: [...portfolioKey(id), "positions"],
    queryFn: () => apiFetch<Position[]>(`/api/portfolios/${id}/positions`),
    refetchInterval: POSITIONS_REFRESH_MS,
  });
}

export function usePerformance(id: string, period: EquityPeriod) {
  return useQuery({
    queryKey: [...portfolioKey(id), "performance", period],
    queryFn: () => apiFetch<Performance>(`/api/portfolios/${id}/performance?period=${period}`),
  });
}

/** `status`: an API status filter ("open", "filled", ...) or "" for every order. */
export function useOrders(id: string, status: string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return useQuery({
    queryKey: [...portfolioKey(id), "orders", status],
    queryFn: () => apiFetch<Order[]>(`/api/portfolios/${id}/orders${qs}`),
  });
}

export function useRiskProfiles() {
  return useQuery({ queryKey: ["risk-profiles"], queryFn: () => apiFetch<RiskProfile[]>("/api/risk-profiles") });
}

export interface TransactionFilters {
  type: string; // a JournalType or "" for all
  from: string; // yyyy-mm-dd or ""
  to: string; // yyyy-mm-dd or ""
}

/** Query string of the filters: day bounds in the portfolio's timezone, ISO with offset. */
export function transactionQuery(filters: TransactionFilters, timeZone: string): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  if (filters.from) params.set("from", zonedDayStart(filters.from, timeZone));
  if (filters.to) params.set("to", zonedDayEnd(filters.to, timeZone));
  return params;
}

export function transactionsCsvUrl(id: string, filters: TransactionFilters, timeZone: string): string {
  const qs = transactionQuery(filters, timeZone).toString();
  return `/api/portfolios/${id}/transactions.csv${qs ? `?${qs}` : ""}`;
}

/** Ledger movements, newest first, one page per `before_seq` cursor. */
export function useTransactions(id: string, filters: TransactionFilters, timeZone: string) {
  return useInfiniteQuery({
    queryKey: [...portfolioKey(id), "transactions", filters, timeZone],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = transactionQuery(filters, timeZone);
      params.set("limit", String(TX_PAGE_SIZE));
      if (pageParam != null) params.set("before_seq", String(pageParam));
      return apiFetch<TransactionPage>(`/api/portfolios/${id}/transactions?${params.toString()}`);
    },
    getNextPageParam: (last) => last.next_before_seq,
  });
}

function useInvalidatePortfolio(id?: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: id ? portfolioKey(id) : portfoliosKey });
    void qc.invalidateQueries({ queryKey: portfoliosKey, exact: true });
    void qc.invalidateQueries({ queryKey: dashboardKey });
  };
}

export function useCreatePortfolio() {
  const invalidate = useInvalidatePortfolio();
  return useMutation({
    mutationFn: (body: PortfolioCreate) => apiFetch<Portfolio>("/api/portfolios", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function usePatchPortfolio(id: string) {
  const invalidate = useInvalidatePortfolio(id);
  return useMutation({
    mutationFn: (body: PortfolioPatch) => apiFetch<Portfolio>(`/api/portfolios/${id}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useCancelOrder(portfolioId: string) {
  const invalidate = useInvalidatePortfolio(portfolioId);
  return useMutation({
    mutationFn: (orderId: string) => apiFetch<Order>(`/api/orders/${orderId}/cancel`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

/** Deposit or withdrawal; the caller passes the Idempotency-Key of the open dialog. */
export function useCashMove(portfolioId: string, kind: "deposit" | "withdraw") {
  const invalidate = useInvalidatePortfolio(portfolioId);
  return useMutation({
    mutationFn: ({ body, key }: { body: CashMove; key: string }) =>
      apiFetch(`/api/portfolios/${portfolioId}/${kind}`, { method: "POST", body, idempotencyKey: key }),
    onSuccess: invalidate,
  });
}

export function useResetPortfolio(portfolioId: string) {
  const invalidate = useInvalidatePortfolio(portfolioId);
  return useMutation({
    mutationFn: ({ body, key }: { body: ResetRequest; key: string }) =>
      apiFetch<ResetResult>(`/api/portfolios/${portfolioId}/reset`, { method: "POST", body, idempotencyKey: key }),
    onSuccess: invalidate,
  });
}

export function useReverseJournal(portfolioId: string) {
  const invalidate = useInvalidatePortfolio(portfolioId);
  return useMutation({
    mutationFn: ({ journalId, reason, key }: { journalId: string; reason: string; key: string }) =>
      apiFetch(`/api/journals/${journalId}/reverse`, { method: "POST", body: { reason }, idempotencyKey: key }),
    onSuccess: invalidate,
  });
}

/** crypto.randomUUID only exists in secure contexts; plain-http LAN access needs a fallback. */
function newKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/**
 * One Idempotency-Key per mount: mount a money dialog per opening, so every opening gets a
 * new key and retries inside the same opening reuse it.
 */
export function useIdempotencyKey(): string {
  const [key] = useState(newKey);
  return key;
}

function priceOf(quote: Record<string, unknown>): string | null {
  for (const field of ["last", "bid", "ask"]) {
    const v = quote[field];
    if (typeof v === "string" && v) return v;
  }
  return null;
}

/**
 * symbol → latest live price (last, else bid/ask) for `symbols`, from the WS quotes.
 * Prices received before `since` changes (e.g. the positions' dataUpdatedAt) are dropped,
 * so the server mark of a refetch is not hidden by an older live price.
 */
export function useLiveQuotes(symbols: string[], since = 0): Record<string, string> {
  const { socket } = useLiveSocket();
  const [state, setState] = useState<{ since: number; prices: Record<string, string> }>({ since, prices: {} });
  if (state.since !== since) setState({ since, prices: {} });
  const key = [...symbols].sort().join(",");
  useEffect(() => {
    if (!socket || !key) return;
    return socket.subscribeQuotes(key.split(","));
  }, [socket, key]);
  useLiveMessage("quote", (quote) => {
    const price = priceOf(quote);
    if (price && symbols.includes(quote.symbol)) {
      setState((prev) => (prev.since === since ? { since, prices: { ...prev.prices, [quote.symbol]: price } } : prev));
    }
  });
  return state.since === since ? state.prices : {};
}
