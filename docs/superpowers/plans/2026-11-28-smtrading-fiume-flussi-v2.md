# Fiume dei flussi v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add replay, order story, agent runs and costs, node health, method lens with price sources, and a «today» width window to the Fiume dei flussi page.

**Architecture:** New read-only admin routes in `admin/routes_flow.py` backed by `smtrading.flow` modules (replay window, order story, health, agent runs via SMTrading's existing Paperclip client with cache, tool families and price sources from the map module); the web page gains a replay bar, a side panel, health rings and a lens overlay on the existing `RiverCanvas`.

**Tech Stack:** as v1 (FastAPI, SQLAlchemy, Redis; React 18, TS, TanStack Query, Canvas 2D, vitest).

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-fiume-flussi-v2-design.md` (read whole; base spec `2026-11-28-smtrading-fiume-flussi-design.md`).

## Global Constraints

- Real data only; read-only; MCP catalog unchanged; admin-only routes (add every new GET to `ADMIN_ONLY_READS`); general view unchanged when nothing is opened.
- Replay: `GET /api/flow/events?from=&to=&limit=` closed window ≤ 24 h, paged by `cursor`; speeds 1×, 10×, 60×, 300×; presets ultima ora, oggi, ieri, ultima seduta USA.
- Width window adds `oggi` (since 00:00 Europe/Rome) to 1/5/15 min: `GET /api/flow/summary?window=1|5|15|today`.
- Order story: `GET /api/flow/order/{order_id}` → decision (sintesi, motivo, confidence, expected via `orders.decision_id`; else «decisione non collegata»), MCP call, children (stop/target), fills (expected vs filled price), Telegram (category and time only, never text), outcome; also `GET /api/flow/mcp/{audit_id}` for a refused call without order (tool, outcome, reason code only, never params beyond symbol).
- Agent runs/costs: impulses `pc → agent:<id>` per Paperclip run (reason, outcome), node badge with today's and month's spend and cap; read via SMTrading's Paperclip client server-side (token never sent to the page), cache ≥ 30 s; Paperclip down → «costi non disponibili», page keeps working.
- Health: `GET /api/flow/health` (cache 15 s) → per node `ok|attenzione|errore|sconosciuto` + one-line Italian reason, from existing sources (stream status `md:stream_status`, broker link status/outages, last get_forecast and export outcomes, Paperclip agent status, Telegram outbox stuck, system health).
- Lens: tool families `ordini · dati di mercato · rischio e previsioni · ricerca · decisioni e attività · messaggi` defined in `flow/map.py` (guard test: every MCP tool has a family); price sources from the market-data router providers (guard test: every provider has a display name), with state, symbols followed, updates per minute (from stream status / existing counters only — if no counter exists, show symbols and state without rate), polled symbols.
- Telegram texts not involved. Plain Italian on the page. Feedback rule «sempre reale»: map/families/sources from one backend module.

## Review Focus

1. Replay of a 24 h window with thousands of events: paged loading, no UI freeze, comets capped.
2. Paperclip unreachable or slow: flow page still renders, costs show «non disponibili», no 5xx.
3. Order without decision link (before 28/11) and refused MCP call: story renders with clear notes.
4. Lens opened while live traffic arrives: comets keep animating in the lens, closing returns to the general view intact.
5. A new MCP tool or market-data provider without family/name: guard tests fail.

---

### Task 1: backend routes and modules

**Files:** `backend/src/smtrading/flow/{replay.py,story.py,health.py,agents.py,families.py}` (or extend existing modules), `admin/routes_flow.py`, `flow/summary.py` (`today`), tests `tests/integration/test_flow_v2_*.py`, `tests/unit/test_flow_families.py`, `tests/integration/test_admin_users.py` (ADMIN_ONLY_READS).
- [ ] Tests first for every route and guard above (fake Paperclip like existing paperclip tests; Redis stream status fixture).
- [ ] Implement; commit `feat(flow): replay, order story, health, agent runs and costs, families and price sources`.

### Task 2: web

**Files:** `web/src/api/hooks/flow.ts`, `web/src/components/flow/RiverCanvas.ts`, `web/src/pages/FlowRiver.tsx`, new components under `web/src/components/flow/` (ReplayBar, StoryPanel, Lens), tests.
- [ ] Tests first (replay controls and preset loading, «oggi» window, story panel for linked/unlinked/refused, health rings and tooltip text, agent badges, lens open/close with families and price sources, Paperclip down note).
- [ ] Implement in the v1 visual language; commit `feat(web): Fiume dei flussi v2`.
