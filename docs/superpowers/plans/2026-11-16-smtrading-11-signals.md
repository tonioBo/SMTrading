# SMTrading 11 · Segnali vicini, indicatori «se chiudesse ora», obiettivo in prima pagina, ultime tracce

> For agentic workers: subagent-driven, one worktree per task, TDD, small commits.

**Goal:** let humans see how close each approved strategy is to an entry, let agents act before the daily close instead of a day late, show the weekly goal on the home page, remove the last «admin» tells from agent-facing events.
**Branch:** feat/plan-11-signals (from main bfb85dc). Ledger: .superpowers/sdd/2026-11-16-smtrading-11-signals/progress.md
**User decisions (2026-11-16 18:40):** all four items.

## Global constraints
- Agent illusion: no simulation/demo/test/admin fingerprints in agent-facing output (`test_mcp_no_fingerprints` stays green; the new MCP field names/texts must pass it).
- Italian UI, comma decimals, Europe/Rome times; viewer role read-only (new admin routes are GET → readable by viewer; role-coverage and activity-log coverage tests stay green).
- Reuse existing code: indicator maths (`get_indicators` implementation in mcp/tools_market.py and wherever RSI/SMA/ATR are computed), backtest strategy definitions (rsi_reversion, sma_cross, breakout, buy_and_hold … in the backtest package), router `get_bars` / `get_quotes`, weekly goal in telegram/weekly_goal.py.
- No migration unless strictly needed.

## Task A · Live («se chiudesse ora») indicators + signal engine (backend)
1. `get_indicators` MCP tool: new optional boolean `include_forming` (default false = today's behaviour). When true and the market of the symbol is open (or crypto), append a provisional bar built from the current quote (open = today's first intraday price if cheaply available else previous close, high/low = max/min with the quote, close = last or mid) to the closed bars before computing; the result carries `provisional: true` and `as_of` (quote ts). Closed-bar values are returned too (`closed: {...}`) so the agent sees both. Same for timeframe 1d and 1wk only; other timeframes ignore the flag (documented in the tool description).
2. Signal engine `smtrading/signals/service.py`: `async def strategy_signals(session, router) -> list[StrategySignal]` for every **approved, not suspended/revoked** strategy. Source of truth: decisions of kind `strategy` (CEO, `details.ref` = STR-xxx, latest status wins) joined to the Quant decision / backtest with the same ref to get strategy type, params, symbols, timeframe, portfolio (find the exact fields in existing decision rows: query the dev DB fixtures/tests or the production-shaped details shown in decisions; never guess silently — if a field is missing, the strategy is listed with `status: "dati incompleti"`). For each symbol: indicator value on the last closed bar, value «se chiudesse ora», entry threshold, distance to the threshold, `triggered_closed` / `triggered_now` booleans, last price, as_of. Supported strategy types = those the backtester supports for long entries; others → `status: "non supportata"`.
3. Admin route `GET /api/signals` (viewer can read) returning the list grouped by portfolio and strategy; cached ~60 s in redis.
4. PM instructions (docs/paperclip-agents/02-portfolio-manager.md): in the sentinel cycles from 21:05 (and 17:05 for EU symbols) use `get_indicators` with `include_forming: true` for the symbols of approved strategies; a signal «se chiudesse ora» allows preparing the proposal before the close (entry before the close, same checklist); the 22:15 cycle confirms on the closed bar and cancels/does not repeat. Keep it short.
## Task B · Web (after A's route exists; may run in parallel against the documented route shape)
1. New page «Segnali» (menu, after «Portafogli»): per portfolio → per strategy (ref, name, params, timeframe) → table of symbols: last price, indicator closed, indicator «se chiudesse ora», threshold, distance, status badge (🟢 scatterebbe ora / 🟡 vicino ≤ 20% of threshold distance / ⚪ lontano), as_of; auto-refresh 60 s; empty state «Nessuna strategia approvata».
2. Home (Dashboard): card «Obiettivo settimanale» = total and per portfolio: Monday opening value → current value, % vs target (+2% minimum), small progress bar; data from a new `GET /api/weekly-goal` backed by telegram/weekly_goal.py (no duplicate maths).
## Task C · Ultime tracce
Agent-facing events/results must not carry `by: "admin"` or similar human markers: cancel events from kill switch/halts (admin/routes_portfolios.py ~391, risk/halts.py ~116, mcp tools_halt) → neutral (`by` omitted or "system"); humans' views (portal, Telegram, activity log) keep the truth. Extend the strict sweep test to cover these events.
## Review Focus
- A: provisional bar on a closed market/holiday must not be added; stale quote (> stale_quote_s) → no provisional value; EU 15-min delayed quotes flagged by as_of.
- A: strategies approved then suspended/revoked must disappear; two CEO decisions for the same ref → latest wins.
- B: page must not hammer providers (one /api/signals call per minute per open tab, cached server-side).
