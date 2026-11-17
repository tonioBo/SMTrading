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

## Task D · Miglioramenti (user decisions 17/11 10:40)
1. **Status-change alerts:** a worker check (with the progress maths) sends Telegram «🟠 Obiettivo <name> in ritardo: x% su y% (tempo trascorso z%)» when a goal's status becomes «in ritardo», and «🟢 Obiettivo <name> di nuovo in linea» when it leaves it (also «🔴 oltre il tetto» for cap). Once per transition (persist last status per goal+period); no daily repeats. If Paperclip is enabled, an «in ritardo» transition also opens an «OBIETTIVO» issue to the CEO (reuse plan-13 issues client; dedup per goal+period).
2. **Risk goals:** new kind `max_loss` («non perdere più del X% nel periodo»): status «nel limite» / «vicino al limite» (loss ≥ 80% of X) / «superato» (loss ≥ X). On «superato» (and on «vicino al limite» once) → Telegram + Paperclip issue to the Risk Manager and the CEO («RISCHIO OBIETTIVO …»), no automatic block. get_goals exposes it; agent docs (00, 01, 03) say how to react (reduce risk, no new openings on that scope until reviewed — the agents' decision).
3. **Benchmark goals:** new kind `beat_index` with `benchmark` ∈ {SPY, QQQ, VWCE.DE} and target «meglio di N punti %» (default 0): progress compares the scope's % with the benchmark's % over the same period, benchmark converted to EUR (USD symbols via the FX at period start and now, through the router). Status «avanti» / «in linea» / «indietro». Shown in page, dashboard, Telegram, get_goals; chart shows the benchmark line (EUR-%) when the goal is shown.
4. **Templates:** «Modelli» button on the Obiettivi page: «Settimanale +2% totale», «Mensile +5% totale», «Trimestrale +10% per portafoglio» (one per active portfolio), «Rischio mensile −5% per portafoglio», «Battere VWCE nel trimestre (totale)» — prefill the dialog, admin confirms.
Migration 0032 if new columns are needed (benchmark, kind values), reversible.
**Contract additions (fixed for D-backend and D-web):** Goal.kind ∈ "min"|"cap"|"max_loss"|"beat_index"; Goal.benchmark: "SPY"|"QQQ"|"VWCE.DE"|null (required for beat_index); target_pct = loss limit % for max_loss (positive number, e.g. "5"), = margin in points for beat_index (e.g. "0"). Progress adds: benchmark, benchmark_pct (EUR, string|null), diff_pp (pct − benchmark_pct), loss_pct (for max_loss: max(0, −pct)); status adds "nel limite"|"vicino al limite"|"superato" (max_loss) and "avanti"|"indietro" (beat_index; "in linea" reused). Equity-history goals items add kind and benchmark, plus `benchmarks: {SYMBOL: [{ts, pct}]}` series when a beat_index goal overlaps the range. New route `GET /api/goals/templates` → [{key, label, body (a Goal create body, portfolio_id filled per active portfolio when per-portfolio)}].
