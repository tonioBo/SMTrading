import type { TradingHalt } from "@/api/types";

/** A TradingHalt for page tests (active, global, set by the CEO agent). */
export function haltFixture(over: Partial<TradingHalt> = {}): TradingHalt {
  return {
    id: 1,
    scope: "global",
    portfolio_id: null,
    portfolio: null,
    active: true,
    reason: "Drawdown oltre il 5%",
    set_by: "agent:a9",
    set_by_name: "CEO SMTrading",
    set_at: "2026-11-14T09:30:00Z",
    cleared_by: null,
    cleared_by_name: null,
    cleared_at: null,
    ...over,
  };
}
