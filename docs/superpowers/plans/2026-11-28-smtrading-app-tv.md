# App «SMTrading TV» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Tizen web app for the Samsung TV that shows SMTrading's TV screens full screen, pairs with the existing 6-digit code, and is driven by the remote; plus the server-side plain-http LAN entry it needs and a build/install script.

**Architecture:** `tv-app/` (new top-level folder): Tizen web app (config.xml, index.html, JS bundle built from the existing TV screen code where possible, remote-key handling), packaged as `.wgt`. Server: an nginx `server` block on a dedicated LAN port (http) that serves only `/api/tv-display/*` with a header-based display token (`X-SMT-TV-Token`) restricted to private client addresses; backend accepts the header on display routes (in addition to the existing cookie). Script `scripts/tv-app.sh` to build, sign (Tizen CLI profile) and install via `sdb` when the tools and the certificate profile exist (documented steps, no secrets in the repo).

**Tech Stack:** Tizen web app (W3C widget), TypeScript/Vite build reused from web/ where practical, nginx, FastAPI.

**Spec:** `docs/superpowers/specs/2026-11-28-smtrading-app-tv-design.md`.

## Global Constraints

- The http LAN entry serves only `/api/tv-display/*` (and nothing of the portal, no static portal, no other API); rejects non-private client addresses (nginx allow 10/8, 172.16/12, 192.168/16; deny all).
- Display token only in the `X-SMT-TV-Token` header (never in URLs); same validation/revocation as the display link; code pairing returns the token to the app in the poll response body only on the http entry when the request carries an `X-SMT-TV-App: 1` header (the browser flow keeps the cookie).
- App: no secrets bundled; stores its token in `localStorage`; remote keys ←/→ screen, OK pause/resume rotation, red/green/yellow/blue shortcuts (health, campaign, flow if allowed, rotation), Back exits (tizen.application.getCurrentApplication().exit()); respects the user's selection from the portal (poll), sounds as in the TV screens.
- «Manda alla TV»: if the app is installed (REST GET /api/v2/applications/<app id> answers), launch it by REST POST; else browser as today.
- Tests for server parts (header auth, private-only, code poll returning token only with the app header on the http entry); web/app logic unit-tested (key mapping, pairing state machine); `npx tsc -b`.
- No contact with the real TV during development; installation only with the owner present (ask on Telegram first).

---

### Task 1: server (header token, http LAN entry, launch app)
### Task 2: Tizen app + build/install script + docs (how to enable developer mode, Samsung certificate profile, install)
