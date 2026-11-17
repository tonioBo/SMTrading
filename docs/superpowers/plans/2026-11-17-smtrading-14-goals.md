# SMTrading 14 · Obiettivi (orizzonti e ambiti diversi)

> For agentic workers: subagent-driven, TDD, small commits. **Deploy only together with the user** (user 17/11 08:25: «mettiamo in linea assieme quando sei pronto»).

**Branch:** feat/plan-14-goals (from main 98b80b6). Ledger: .superpowers/sdd/2026-11-17-smtrading-14-goals/progress.md
**User decisions:** goals list with horizon + scope; goals DRIVE the agents (read from SMTrading; CEO splits long goals into stages; behind → new strategy research; risk always first).

## Global constraints
- Italian UI, comma decimals, Europe/Rome. Viewer reads, admin writes (role + activity-log coverage tests).
- Agent illusion: the MCP tool output and agent docs contain no simulation words; amounts in EUR.
- The existing weekly goal must keep working with zero action: migrate the current weekly goal setting (on/pct) into a recurring weekly goal on the total.
- Reuse equity snapshots (day_open), valuation, telegram/weekly_goal.py maths (refactor into goals/progress.py; weekly_goal becomes a thin wrapper or is removed with all callers updated).

## Contract (fixed; B and C build against it)
Goal: `{id, name, horizon: "week"|"month"|"quarter"|"year"|"custom", start_date, end_date (custom only; others computed), recurring: bool (week/month/quarter/year roll over automatically), scope: "total"|"portfolio", portfolio_id|null, target_pct (string, e.g. "2"), kind: "min"|"cap", active, created_at, updated_at}`.
Progress: `{goal_id, name, horizon, scope, portfolio_id, portfolio_name, kind, period_start, period_end, start_eur, current_eur, target_eur, pct, target_pct, gap_eur, elapsed_pct (time elapsed in the period, business days), status: "raggiunto"|"in linea"|"in ritardo"|"non iniziato"|"concluso" , as_of}` (numbers as strings; null when no data).
Status rule: raggiunto if pct ≥ target (min) ; in ritardo if pct < target × elapsed_pct − 0,5 pp (min); for kind cap: «superato» when pct > target (add to enum: "oltre il tetto").
Admin API: `GET /api/goals`, `POST /api/goals`, `PATCH /api/goals/{id}`, `DELETE /api/goals/{id}` (soft: active=false), `GET /api/goals/progress` (active goals, current period).
MCP: `get_goals` (no params; returns progress of the active goals visible to the agent: total goals + goals of its portfolios; field names as in Progress minus internal ids except portfolio_id).

## Task A · Backend goals core (first)
Model + migration 0031 (reversible; data migration from the weekly goal app setting), service (CRUD, period computation incl. quarter/year/custom, recurring roll-over), progress maths (refactor of weekly_goal), admin routes, activity-log catalog entries, Telegram: daily/weekly summaries show one line per active goal (replacing the single weekly line); end-of-period summary line «Obiettivo <name>: raggiunto/non raggiunto (x% su y%)» sent once when a period ends; pre-open check line uses goals. Tests.
## Task B · MCP + agent instructions (after A)
`get_goals` tool (mcp tools module + DTO + agent-safe sweep test; the MCP catalog fingerprint changes → the automatic session refresh from plan 13 will reset agent sessions on deploy: expected). Agent docs: 00-regole-comuni (replace the hard-coded «almeno +2%» paragraph with: objectives come from `get_goals`; minimum to exceed unless kind cap; risk first), 01-ceo (Piano per gli obiettivi: read get_goals at every OBIETTIVO task and in the Friday review; split month/quarter/year into stages; behind → RICERCA STRATEGIA as today), 02-portfolio-manager (BUONGIORNO and OBIETTIVO task use get_goals: progress per goal), 09-notifier (BUONGIORNO lines per goal). docs/paperclip-mcp.md entry.
## Task C · Web (after A's contract; parallel with B)
Page «Obiettivi» (menu Trading after Strategie): list with progress bars and status badges, create/edit dialog (horizon, dates for custom, recurring, scope/portfolio, %, kind), deactivate. Dashboard: replace the single «Obiettivo settimanale» card with «Obiettivi» showing every active goal compactly. Andamento chart: goal lines for active goals of the shown scope whose period overlaps the range (dashed, labelled).
## Review Focus
- Periods: quarter/year boundaries, custom spanning weekends/holidays, recurring roll-over at midnight Europe/Rome, no snapshot at period start (use first snapshot after start).
- Portfolio goal on archived portfolio → hidden/inactive.
- Agents: get_goals never exposes other agents' portfolios.
