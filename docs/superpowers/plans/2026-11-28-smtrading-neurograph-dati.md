# Neurograph data & richer decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install get_forecast with events, let agents record confidence/expected result and link orders to decisions, and send SMTrading's daily decisions/orders/fills to Neurograph.

**Architecture:** Merge Luca's branch; add nullable columns (`decisions.confidence`, `decisions.expected`, `orders.decision_id`) with optional MCP parameters; a worker job builds JSON Lines per type and Rome day and POSTs them to Neurograph's ingest endpoint, keeping per-day send state for idempotence and backfill; the flow map shows the export as `db → ng`.

**Tech Stack:** Python 3.12, FastAPI/MCP server, SQLAlchemy async + Alembic, httpx, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-neurograph-dati-design.md` (read whole).

## Global Constraints

- Agent instruction text (00 common doc), verbatim: «In ogni `log_decision` indica `confidence` (da 0 a 100, quanto sei sicuro) e, quando la decisione riguarda un risultato di mercato, `expected` con `return_pct` (rendimento atteso in %) e `horizon_days` (in quanti giorni). Prima di ogni ordine registra la decisione e passa il suo `decision_id` all'ordine.» — must keep `tests/unit/test_agent_docs_appendix.py` green (size limits).
- MCP changes allowed only: get_forecast (Luca's branch), `log_decision` (+`confidence`, `expected`), order-creating tools (+`decision_id`). Update the frozen fingerprints once (`tests/unit/test_mcp_catalog_frozen.py`, `tests/integration/test_admin_auto_rules.py`); `tests/unit/test_flow_map.py` must stay green (tool names unchanged).
- `confidence` int 0..100; `expected` = `{return_pct: number, horizon_days: int 1..365}`; `decision_id` must exist and belong to the calling agent, else `INVALID_ORDER` with `details.field = "decision_id"`. All optional; existing calls unchanged.
- Export: URL `SMT_NEUROGRAPH_EXPORT_URL` default `http://192.168.4.4:18088/api/dati/smtrading`; key `SMT_NEUROGRAPH_EXPORT_KEY` (never logged/printed; no key → job disabled); path `/{decisioni|ordini|esecuzioni}/{AAAA-MM-GG}`; headers `Authorization: Bearer`, `Content-Type: application/x-ndjson`, `X-SMT-Export-Id: smtrading:{tipo}:{giorno}:{versione}`, `X-SMT-Rows`; body ≤ 64 MB; responses 201/200 = confirmed; 400 = don't retry same version, Telegram system alert with Neurograph's message; 401 = stop the run, Telegram alert; 503/5xx/timeout = retry next night. Nightly after 00:00 Europe/Rome; backfill up to 30 days, first run from 2026-11-23; schema v1 with Italian field names as in the spec/page + `decisione` on orders; only `total_portfolio_ids()`; decimals as strings; ISO UTC; day = Rome day.
- Flow map: export events `export:<tipo>:<giorno>:<versione>`, path `["db","ng"]`, hop «esportazione <tipo>», outcome from the send result; link `db|ng` in the map.
- Telegram texts impersonal, plain Italian.

## Review Focus

1. An agent passing a `decision_id` of another agent or a non-existent id → refused, nothing stored.
2. Neurograph down for several nights → the next successful night sends all missing days in order, each once.
3. A 400 on one type/day doesn't block the other types or the following days.
4. Hidden test portfolios' orders/fills and archived portfolios never exported.
5. The key never appears in logs, errors, Telegram, the flow page or job_runs.

---

### Task 1 (controller): merge `origin/get-forecast-eventi`

- [ ] Merge into the feature branch; keep efea71e's unexpected-shape handling; run `tests/integration/test_mcp_forecast.py`, catalog tests (update fingerprint), `tests/unit/test_flow_map.py`, `tests/unit/test_agent_docs_appendix.py`; commit.

### Task 2: richer decisions and order → decision link

**Files:** migration `backend/alembic/versions/0049_decision_confidence_order_link.py` (or next number); `backend/src/smtrading/activity/models.py` (Decision), `engine/models.py` (Order), `mcp/tools_activity.py` (log_decision), `mcp/tools_orders.py` (order tools), `engine/service.py` or request models (carry `decision_id`), `docs/paperclip-agents/00-*.md` (instruction text), `docs/paperclip-mcp.md`; tests in `tests/integration/test_mcp_decisions*.py`, `tests/integration/test_mcp_orders*.py`, migrations test.

**Interfaces — Produces:** `Decision.confidence: int | None`, `Decision.expected: dict | None`, `Order.decision_id: int | None` (FK decisions.id, ON DELETE SET NULL); `get_decisions` returns the two fields.

- [ ] Tests first: `log_decision` with `confidence=70, expected={"return_pct": 2.5, "horizon_days": 5}` stored and returned; `confidence=101` → `INVALID_ORDER` field confidence; `place_order(..., decision_id=<own>)` stores it; other agent's id or unknown id → `INVALID_ORDER` field decision_id and no order; calls without the new params unchanged; migration up/down.
- [ ] Implement; publish nothing (agent docs are published at release by the controller).
- [ ] Commit `feat(mcp): decisions carry confidence and expected result; orders link to their decision`.

### Task 3: nightly export to Neurograph + flow map

**Files:** `backend/src/smtrading/neurograph/export.py` (build rows), `backend/src/smtrading/neurograph/sender.py` (HTTP + state), job registration in the worker scheduler (pattern of existing nightly jobs), state persistence (new small table via migration, or `job_runs` claims if they fit), CLI command `smtrading.cli neurograph-export --day AAAA-MM-GG [--type …] [--version N]` for the one-day test, `common/config.py` (`neurograph_export_url`, `neurograph_export_key`), `.env.example`, `flow/map.py` + `flow/events.py` (export events), tests.

**Interfaces — Produces:** `async def build_rows(session, kind: Literal["decisioni","ordini","esecuzioni"], day: date) -> list[dict]`; `async def send_day(session, client, kind, day, version=1) -> SendResult` (status, message); `async def run_nightly(now)`.

- [ ] Tests first (fake Neurograph HTTP server like `tests/integration/test_mcp_forecast.py`): headers exact (id, rows, content type, bearer); 201 then a second run sends nothing; 200 «già presente» counts as confirmed; 400 on ordini doesn't stop decisioni/esecuzioni nor next day and sends one Telegram system alert; 401 stops and alerts once; 5xx → retried next run; 3 nights down → next run sends all missing days in date order; only campaign portfolios; `decisione` filled from `orders.decision_id`; no key → job does nothing; key never in logs (caplog) or job state; export events appear in `flow_events` with path `["db","ng"]`; map has link `db|ng`.
- [ ] Implement; job disabled without key.
- [ ] Commit `feat(neurograph): nightly export of decisions, orders and fills; shown in the flow map`.

### Task 4 (controller): release

- [ ] Full backend + web suites; whole-branch review; merge, push; agents idle: build, backup, migrate, recreate; publish the 00 agent doc (new version); check the session reset message; with the key in `.env`: one-day test (`--day 2026-11-27`), confirm with Luca's ingest log; then enable nightly; Telegram report.
