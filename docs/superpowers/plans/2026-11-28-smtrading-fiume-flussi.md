# Fiume dei flussi Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A portal page «Fiume dei flussi» that draws SMTrading's real flows (agents → Paperclip → MCP → engine/data → outputs → brokers) as a living Sankey river, fed by read-only backend routes polled every ~2 s.

**Architecture:** A new backend package `smtrading.flow` owns the node map (the single source of truth for nodes and links), translates existing records (mcp_audit_log, orders, fills, auto_trades, telegram_outbox) into path events, and aggregates a summary; `admin/routes_flow.py` serves three admin-only GETs. The web page renders the approved sketch (`docs/design-sketches/fiume-pro.html`) in a Canvas 2D class driven by those routes. No write path, no MCP change.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PostgreSQL, pytest + testcontainers; React 18, TypeScript, TanStack Query 5, Vite, vitest.

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-fiume-flussi-design.md` (read it whole, including «Decisioni prese dopo la lettura del costruttore» and «Sempre aggiornata»). Visual reference: `docs/design-sketches/fiume-pro.html` + `flussi-shared.js`.

## Global Constraints

- Real data only, no demo/simulated mode in the product page.
- Read-only: no table writes, no new events published, **MCP catalog unchanged** (`tests/unit/test_mcp_catalog_frozen.py`, `tests/integration/test_admin_auto_rules.py::test_mcp_unchanged_after_routes` stay green with the current fingerprint `668d1432822dc806bbd87a5ed6f5cf9c89130d988d5fa56723789575b88237b9`).
- Routes: `GET /api/flow/map`, `GET /api/flow/events`, `GET /api/flow/summary`, router `dependencies=[Depends(require_admin)]`, added to `ADMIN_ONLY_READS` in `tests/integration/test_admin_users.py`.
- MCP rows: only agents' calls; exclude `method LIKE 'admin:%'`; from `params` only `symbol` or the first of `symbols`.
- Portfolio filter for orders/fills/auto_trades: `portfolios.models.total_portfolio_ids()` (hidden test and archived portfolios excluded). MCP calls are not filtered by portfolio.
- Event ids stable: `mcp:<id>`, `order:<id>:sent`, `order:<id>:rejected`, `fill:<id>`, `tg:<id>`, `auto:<id>`; `ts` ISO UTC; MCP event `ts = created_at − latency_ms`.
- Cursor = time: `events?after=<iso>` returns events with `ts > after − 10 s` (client dedupes by id); no `after` → last 15 minutes; `limit` 1..2000, default 500; response `{events, cursor, now}` where `cursor` = max ts returned (or `after` if none).
- Telegram: rows with `source LIKE 'agent:%'` are not events (already the `send_telegram` call); others (`system`, `admin:*`) → `Motore → Telegram` with the category as label; only `status = 'sent'`.
- Code in English; all page text in plain Italian; no new web dependency.
- The page polls only while the tab is visible (`document.visibilityState === 'visible'`).

## Review Focus

1. A night or a weekend with no traffic: the page shows the empty-state note and thin bands, never a blank or broken canvas — test in Task 3.
2. A new MCP tool or broker kind added later without a destination/label: a guard test fails — tests in Task 1 (the spec's «Sempre aggiornata»).
3. An order that is sent, then filled minutes later, across two polls: both events appear once each, in order — test in Task 2.
4. A hidden test portfolio trading (intraday drill) while the page is open: its orders/fills never appear, while its agent's MCP calls do — test in Task 2.
5. The backend unreachable or a 5xx during polling: the page keeps the last river, shows «dati non aggiornati» and recovers by itself — test in Task 3.

---

### Task 1: Flow map (nodes, links, tool destinations, guards)

**Files:**
- Create: `backend/src/smtrading/flow/__init__.py`, `backend/src/smtrading/flow/map.py`
- Test: `backend/tests/unit/test_flow_map.py`, `backend/tests/integration/test_flow_map_db.py`

**Interfaces:**
- Produces:
  - `TOOL_DEST: dict[str, str]` — MCP tool name → destination node key, one of `"eng" | "px" | "ng" | "db" | "tg" | "mcp"` (`"mcp"` = the call ends at the MCP).
  - `ONE_WAY_DESTS = {"eng", "tg"}` — destinations whose MCP path has no return hops.
  - `BROKER_LABELS: dict[str, str]` — every broker kind known to the code → Italian label: `alpaca_paper` «Alpaca (prova)», `alpaca_live` «Alpaca reale», `etoro_demo` «eToro (demo)», `etoro_live` «eToro reale», `t212_demo` «Trading 212 (demo)», `t212_live` «Trading 212 reale».
  - `STATIC_NODES` — keys `pc, mcp, rules, ng, eng, db, px, sync, sim, tg` with `label`, `kind` (colour family: `agent|core|db|data|ng|tg|broker`), `column` (0..5 as in the sketch: agents 0; pc 1; mcp, rules 2; ng, eng, db, px 3; sync, sim, tg 4; brokers 5) and an Italian `description` (texts from `flussi-shared.js`).
  - `async def flow_map(session) -> dict` → `{"nodes": [{key,label,kind,column,description}], "links": [[a,b], ...]}`: agent nodes are the rows of `agents` (key `agent:<uuid>`, label = agent name, kind `agent`, column 0, description from its role if available else «Agente»), broker nodes are `broker:<kind>` for each distinct `broker_links.kind` present; links = every pair that any event can produce (agent–pc, pc–mcp, mcp–each dest, eng–sync, sync–each broker, eng–sim, eng–tg, px–rules, rules–eng).
- [ ] **Step 1: Write the failing tests**
```python
async def test_every_mcp_tool_has_a_destination():
    names = {t.name for t in await build_mcp().list_tools()}
    assert names <= set(TOOL_DEST), sorted(names - set(TOOL_DEST))
    assert set(TOOL_DEST.values()) <= {"eng", "px", "ng", "db", "tg", "mcp"}
    assert TOOL_DEST["get_forecast"] == "ng" and TOOL_DEST["place_order"] == "eng" and TOOL_DEST["get_bars"] == "px"

def test_every_broker_kind_has_a_label():
    from smtrading.admin.routes_brokers import KINDS
    from smtrading.brokers.live import LIVE_KINDS
    assert set(KINDS) | set(LIVE_KINDS) <= set(BROKER_LABELS)
```
  plus an integration test `test_flow_map_from_db`: two agents and links of kinds `alpaca_paper` and `etoro_demo` → nodes contain both `agent:<id>` with their names and exactly `broker:alpaca_paper`, `broker:etoro_demo` (no t212), links contain `["sync","broker:alpaca_paper"]`, columns as above.
- [ ] **Step 2:** run `uv run pytest -q tests/unit/test_flow_map.py tests/integration/test_flow_map_db.py` → FAIL (module missing).
- [ ] **Step 3:** implement `flow/map.py` (mapping by tool name; when unsure of a tool, its module decides: `tools_orders`→eng, `tools_market`/`tools_watch` market reads→px, `tools_forecast`→ng, `tools_telegram`→tg, portfolio/backtest/research/activity/goals/docs/alerts/halt→db).
- [ ] **Step 4:** tests PASS; `uv run ruff check src tests`.
- [ ] **Step 5:** commit `feat(flow): node map, MCP tool destinations and guards for the Fiume dei flussi`.

### Task 2: Flow events, summary and admin routes

**Files:**
- Create: `backend/src/smtrading/flow/events.py`, `backend/src/smtrading/flow/summary.py`, `backend/src/smtrading/admin/routes_flow.py`
- Modify: `backend/src/smtrading/api/main.py` (include router), `backend/tests/integration/test_admin_users.py` (`ADMIN_ONLY_READS` += the 3 GETs), any other route catalog test that lists routes (run `tests/integration/test_admin_users.py tests/integration/test_user_activity.py` to find them)
- Test: `backend/tests/integration/test_flow_events.py`, `backend/tests/integration/test_admin_flow.py`

**Interfaces:**
- Consumes: Task 1 `TOOL_DEST`, `ONE_WAY_DESTS`, `flow_map`.
- Produces:
  - `async def flow_events(session, *, after: datetime | None, limit: int, now: datetime) -> list[dict]` → events sorted by `ts`, each `{id, ts, kind, who, path, hops, outcome, latency_ms, symbol}` with `kind` in `mcp|order_sent|order_rejected|fill|telegram|auto`.
    - MCP: `path = [agent:<id>, "pc", "mcp", dest] + (["mcp","pc"] if dest not in ONE_WAY_DESTS and dest != "mcp" else [])`, for dest `"mcp"`: `[agent, "pc", "mcp", "pc"]`; `hops` = tool name for the forward hops, `"risposta"` for return hops; `outcome = "ok"` if row outcome is `ok`, else `"error"`.
    - order_sent (orders with `broker_sent_at`): `["eng","sync","broker:<kind>"]`, hops `["invio", "ordine <SYMBOL>"]`; order without link, filled: not an order_sent.
    - fill: with link → `["broker:<kind>","sync","eng"]` hops `["eseguito <SYMBOL>","registrazione"]`; without link → `["eng","sim"]` hop `["esecuzione simulata <SYMBOL>"]`.
    - order_rejected (`status = rejected`, ts = `updated_at`): `["eng","sync","broker:<kind>"]` or `["eng"]`-only rejections (no link) as `["mcp","eng"]` hop `["rifiutato"]`; outcome `"error"`.
    - auto: `auto_trades.trigger_at` → `["px","rules","eng"]` hops `["scatto <SYMBOL>","entrata"]`.
    - telegram: per Global Constraints.
    - `who`: agent node key for MCP, `"eng"` for order/fill/telegram, `"rules"` for auto.
  - `async def flow_summary(session, *, window_min: int, now: datetime) -> dict` → `{links: {"a|b": {calls, errors, top_tools: [..3]}}, nodes: {key: per_min}, head: {calls_per_min, orders_today, errors_window, mcp_latency_ms}}`, computed from `flow_events(after=now−window)` (one translation path; `limit` 5000).
  - Routes in `routes_flow.py`: `GET /api/flow/map`, `GET /api/flow/events?after=&limit=`, `GET /api/flow/summary?window=1|5|15` (422 otherwise).
- [ ] **Step 1: Write the failing tests** in `test_flow_events.py`:
  - `test_mcp_call_becomes_a_path`: an `mcp_audit_log` row (`get_forecast`, outcome ok, latency 900, params `{"symbol":"SPY","horizon":5}`) → one event `id "mcp:<id>"`, path `[agent:<id>,"pc","mcp","ng","mcp","pc"]`, `symbol "SPY"`, `ts == created_at − 900 ms`, no other param keys in the event.
  - `test_admin_rows_are_not_events`: method `admin:order.place` → no event.
  - `test_error_outcome_marks_the_event`: outcome `INVALID_ORDER` → `outcome "error"`.
  - `test_order_sent_then_filled_across_two_polls`: order sent at T, poll with `after=None` → `order:<id>:sent`; fill at T+120 s, poll with `after=cursor` → `fill:<fid>` only once (and `order:<id>:sent` may reappear only inside the 10 s overlap — assert ids unique per response).
  - `test_hidden_portfolio_orders_are_hidden_but_agent_calls_show`: portfolio without `agent_portfolios` rows → its order/fill not returned; an agent MCP row is returned.
  - `test_telegram_agent_rows_not_counted_twice`: outbox `source "agent:<id>"` → no event; `source "system"`, sent → `["eng","tg"]`.
  - `test_simulated_fill_goes_to_sim`.
  - `test_limit_and_order`: 30 rows, `limit=10` → 10 oldest after the cursor, sorted.
  In `test_admin_flow.py`: admin gets 200 on the three routes; a viewer gets 403; `window=7` → 422; the MCP fingerprint is unchanged after including the router.
- [ ] **Step 2:** run them → FAIL.
- [ ] **Step 3:** implement (one SQL query per source, bounded by `after − 10 s` and `limit`; join `broker_links` for kind; `total_portfolio_ids()` subquery for orders/fills/auto_trades). Add an index migration only if `EXPLAIN` on the live-size data shows a sequential scan on `orders.updated_at` or `fills.created_at` that matters (state the finding in the report either way).
- [ ] **Step 4:** run Task 1 + Task 2 tests, `tests/integration/test_admin_users.py`, `tests/integration/test_user_activity.py`, `tests/unit/test_mcp_catalog_frozen.py`, `tests/integration/test_admin_auto_rules.py::test_mcp_unchanged_after_routes` → PASS; ruff clean.
- [ ] **Step 5:** commit `feat(flow): real flow events, summary and admin routes`.

### Task 3: Web page «Fiume dei flussi»

**Files:**
- Create: `web/src/api/hooks/flow.ts`, `web/src/components/flow/RiverCanvas.ts` (renderer class, no React), `web/src/pages/FlowRiver.tsx`
- Modify: `web/src/routes.tsx` (`{ path: "fiume-flussi", element: <FlowRiver /> }`), `web/src/components/Sidebar.tsx` (entry «Fiume dei flussi» right after «Log MCP»), `web/src/components/Layout.test.tsx` (expected menu labels), `web/src/api/types.ts` (types below)
- Test: `web/src/pages/FlowRiver.test.tsx`, `web/src/components/flow/RiverCanvas.test.ts`

**Interfaces:**
- Consumes: Task 2 routes and shapes.
- Produces (TS):
  - `type FlowNode = {key:string; label:string; kind:string; column:number; description:string}`; `type FlowMap = {nodes: FlowNode[]; links: [string,string][]}`; `type FlowEvent = {id:string; ts:string; kind:string; who:string; path:string[]; hops:string[]; outcome:"ok"|"error"; latency_ms:number|null; symbol:string|null}`; `type FlowSummary` as Task 2.
  - Hooks: `useFlowMap()` (staleTime 60 s), `useFlowEvents()` (polls every 2000 ms with the cursor kept in a ref, only when the tab is visible, returns `{events: FlowEvent[] /* new since last poll, deduped by id */, stale: boolean}`), `useFlowSummary(window: 1|5|15)` (every 10 s).
  - `class RiverCanvas { constructor(canvas: HTMLCanvasElement, timeline: HTMLCanvasElement); setMap(m: FlowMap): void; setSummary(s: FlowSummary): void; push(events: FlowEvent[], serverNow: string): void; setPaused(b: boolean): void; setSpeed(x: 0.5|1|2): void; setFollow(b: boolean): void; onHover(cb: (h: {node?: string; link?: string} | null) => void): void; destroy(): void }` — drawing, layout, particles, comets, flashes and timeline exactly as the sketch, but band widths from `summary.links[*].calls` and comets from pushed events replayed at `ts + 2.5 s` relative to the server clock (`serverNow` offset), spread over the poll interval.
  - Page: header (title, plain-Italian one-liner, counters from `summary.head`), controls (pausa, velocità ½/1/2, larghezza su 1/5/15 min, «segui un ordine» = highlights the next `order_sent`/`place_order` path with the sketch's explanation text), hover card (node description + per-minute count + last real pass «ora · strumento»; link: calls/min, top tools, errors, prevailing direction), live diary (last 8 events: «HH:MM:SS agente/sorgente strumento · NN ms» or «✕ errore»), 2-minute timeline by origin (agenti · regole automatiche · sistema), empty state «Nessuna chiamata negli ultimi N minuti» when the window has none, stale banner «dati non aggiornati, nuovo tentativo…» when a poll fails, canvas `aria-label` with a one-sentence numeric summary.
- [ ] **Step 1: Write the failing tests**: `FlowRiver.test.tsx` — renders the menu title and counters from mocked hooks; with zero events shows «Nessuna chiamata negli ultimi 1 minuti»; with a failing events query shows «dati non aggiornati»; changing «larghezza su 5 min» calls `useFlowSummary(5)`. `RiverCanvas.test.ts` (jsdom canvas stub) — `setMap` creates one node per map node and no node for keys absent from the map; `push` of the same event id twice schedules one comet; an event whose path contains an unknown node is ignored without throwing. `Layout.test.tsx` updated for the new menu entry.
- [ ] **Step 2:** `cd web && npx vitest run src/pages/FlowRiver.test.tsx src/components/flow src/components/Layout.test.tsx` → FAIL.
- [ ] **Step 3:** implement (port the sketch's drawing code into `RiverCanvas`; nodes/links/labels/colours from the map, never hard-coded; colours per `kind` as in the sketch).
- [ ] **Step 4:** tests PASS; `npx tsc --noEmit`; `npx vitest run` (whole web suite) PASS.
- [ ] **Step 5:** commit `feat(web): Fiume dei flussi page on real flows`.

### Task 4: Release (controller)

- [ ] Full backend suite (`uv run pytest -q -p no:cacheprovider`) and web suite green; whole-branch review.
- [ ] Merge to `main`, push with `git.exe`; with Paperclip agents idle: build (backend with `--build-context agentdocs=docs/paperclip-agents`, web), `pg_dump` backup, tag `:local`, `docker compose run --rm migrate`, recreate `api worker web scalper`.
- [ ] Live check at `https://192.168.30.7/fiume-flussi`: map shows the real agents and broker links; today's real events appear (MCP calls, orders if any); no errors in api logs; Telegram report.
