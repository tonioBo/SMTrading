# Schermo TV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the portal, pair the Samsung smart TV once, then send SMTrading TV screens (Fiume dei flussi, Campagna e portafogli, Salute e avvisi, rotation) to it automatically, via a revocable read-only display link.

**Architecture:** `smtrading.tv` package: Samsung remote-control client (WebSocket on port 8002, token auth; launch the Tizen browser with a URL; power key), stored settings (TV address, encrypted token, current display selection), signed display links; admin routes for the portal section; display routes authorised by the display link only; web: portal section «Schermo TV» and full-screen TV pages.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, existing `websockets`/`httpx`, existing secret encryption of the portal; React 18 + TS.

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-schermo-tv-design.md` (read whole).

## Global Constraints

- TV address never hard-coded: configured in the portal section or found by a LAN discovery the admin starts (scan of the portal host's /24 for `http://<ip>:8001/api/v2/` answering `"type":"Samsung SmartTV"`, short timeouts, admin-only).
- TV token is a secret: stored encrypted like other secrets (find the existing pattern, e.g. broker link config encryption), never in responses, logs, Telegram, flow events.
- Display link: signed (HMAC with an existing server secret or a new random secret stored like other secrets), scope «tv-display» only, revocable from the portal (rotating the secret or a stored link id), lifetime long (until revoked). Display routes accept only that link (cookie or query token) and expose only TV screen data; nothing else of the portal.
- Remote: «Collega la TV» (pairing → the TV asks to allow; store token), «Manda alla TV» (power on if standby when possible, launch browser on the display URL), «cambia schermata», «rotazione» (interval 15–300 s), «spegni lo schermo». The TV page polls the selection every 3–5 s so screen changes don't reopen the browser.
- Fallback when launch fails: show the display URL and a QR code (generate SVG server-side or in the web without new heavy deps; check what exists).
- TV screens: 3840×2160 friendly, large type, high contrast, no interaction, auto-refresh; data from existing routes/modules (flow v2 summary/health, campaign/portfolios as in Roadmap/dashboard); no secrets, alert titles only.
- Per-user reserved area (spec §«Area riservata per utente»): every portal user (admin and viewer) manages only their own TVs, links and selection (rows keyed by user id); admins additionally list/revoke all TVs; the own-TV actions are allowed to viewers (add them to the viewer-allowed unsafe routes in `tests/integration/test_admin_users.py` and to the user-activity catalog); admin-only list route in `ADMIN_ONLY_READS`; the screens show the same data the user can see; MCP catalog unchanged.
- Feedback rules: roadmap item updated; the new flow «SMTrading → TV» appears in the Fiume dei flussi map (node `tv`, link from the web/api to it) with events when a screen is sent.
- Plain Italian on screens and portal.

## Review Focus

1. TV off/unreachable or the user refuses the prompt: clear message, nothing hangs (timeouts ≤ 10 s).
2. Display link leaked: it opens only TV screens; revoking it locks the TV out immediately.
3. TV browser left open for days: no memory growth, recovers after api restarts.
4. A model that ignores the browser launch: QR/URL fallback works.
5. Token rotation: if the TV forgets the pairing, «Collega la TV» again replaces the token cleanly.

---

### Task 1: backend (`smtrading.tv`, routes, display data)
- [ ] Tests first with a fake Samsung WebSocket server (pairing returns a token; launch command recorded; power key), discovery against a fake HTTP endpoint, display-link auth (valid, revoked, wrong scope), display data shapes, secrets never in responses/logs, route catalogs.
- [ ] Implement; commit `feat(tv): pair the Samsung TV and send SMTrading screens to it`.

### Task 2: web (portal section + TV pages)
- [ ] Tests first (section states: not configured, discovered, paired, sending, failed with QR; TV pages render with data, empty/error states, rotation, selection polling).
- [ ] Implement; commit `feat(web): Schermo TV`.
