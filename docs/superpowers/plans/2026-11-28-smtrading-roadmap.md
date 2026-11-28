# Roadmap page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A portal page «Roadmap» readable by every portal user, from a curated YAML plus live numbers, guarded so it never falls behind the specs and plans.

**Architecture:** `smtrading.roadmap` package holds `roadmap.yaml` (package data) and a loader/validator; `GET /api/roadmap` merges it with live numbers; the web page renders a plain-language vertical timeline.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, PyYAML (check it is already a dependency; if not, use JSON instead of YAML — no new dependency), pytest; React 18 + TS + TanStack Query + vitest.

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-roadmap-design.md` (read whole).

## Global Constraints

- Readable by admin and viewer users (router with the normal access dependency, NOT `require_admin`; not in `ADMIN_ONLY_READS`). No write routes.
- Stage statuses exactly: `fatto`, `in_corso`, `prossimo`, `futuro`; owners exactly: `smtrading`, `neurograph`, `entrambi`, `matteo`.
- Every file in `docs/superpowers/specs/*.md` and `docs/superpowers/plans/*.md` must be referenced by at least one item (`refs:` list of repo paths) — guard test reading the repo docs folder (skip gracefully only if the folder is absent in the container image; the test runs in the repo).
- Live numbers: campaign day N of total (campaign dates from the roadmap file, 2026-11-23 → 2026-12-23), campaign equity total vs goal target (existing goals/equity_snapshots data, `total_portfolio_ids()`), drills done/planned (items with `kind: esercitazione`), days to the go/no-go (2026-12-23), Neurograph tools live (`flow.map.TOOL_DEST` values == "ng").
- Plain Italian; every acronym explained in the glossary; no secrets, no IPs.
- Menu «Roadmap» visible to all users; page works at phone width.
- `tests/unit/test_mcp_catalog_frozen.py` unchanged (no MCP change).

## Review Focus

1. A viewer user opens the page → 200 and full content.
2. No goal configured or no equity snapshot yet → live block shows «—» with a short note, never an error.
3. A new spec added without a roadmap item → guard test fails with the file name.
4. Long stage texts and many items on a phone → readable, no horizontal scroll.
5. Dates in the past still marked `prossimo` → validator warns in a test (stale status).

---

### Task 1: roadmap data, loader, guard and route

**Files:** `backend/src/smtrading/roadmap/__init__.py`, `roadmap/roadmap.yaml` (or `.json`), `roadmap/service.py`, `admin/routes_roadmap.py`, `api/main.py`, packaging of the data file (check `pyproject.toml` package data), tests `tests/unit/test_roadmap_file.py`, `tests/integration/test_roadmap_route.py`.
**Produces:** `load_roadmap() -> dict` (validated), `async def roadmap_view(session, now) -> dict` = file + `live`.
- [ ] Tests first (validator, guard on specs/plans references, stale-status check, route for admin and viewer, live block with and without goals).
- [ ] Write the content: stages and items per the spec (foundations = plans 1-17; real tests and watch; December campaign with the drills from `docs/readiness/2026-12-campagna-prontezza.md` §3 with their real status; intraday and automatic rules; Neurograph (T0, events, get_forecast, export, next tools: bracket odds, regime, portfolio risk); Fiume dei flussi; go/no-go 23/12; after: intraday with real-time prices, real money, options), glossary.
- [ ] Commit `feat(roadmap): curated roadmap with live numbers and guards`.

### Task 2: web page

**Files:** `web/src/api/hooks/roadmap.ts`, `web/src/pages/Roadmap.tsx`, `routes.tsx`, `Sidebar.tsx` (entry «Roadmap», all users), `Layout.test.tsx`, tests `web/src/pages/Roadmap.test.tsx`.
- [ ] Tests first (renders summary, live numbers, «sei qui» on the first `in_corso` stage, expanding a stage shows items, live missing → «—», glossary).
- [ ] Implement a calm, readable vertical timeline in the portal theme.
- [ ] Commit `feat(web): Roadmap page`.
