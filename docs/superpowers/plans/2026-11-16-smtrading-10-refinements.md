# SMTrading 10 · Rifiniture — Implementation Plan

> For agentic workers: subagent-driven, one worktree per task, TDD, small commits.

**Goal:** close the carries found by the plan-9 final review and the first trading day.
**Branch:** feat/plan-10-refinements (from main 2efc0ce). Ledger: .superpowers/sdd/2026-11-16-smtrading-10-refinements/progress.md
**User decisions (2026-11-16):** manual admin orders look like normal orders to agents; crypto news from the public CoinDesk + Cointelegraph RSS feeds (no key).

## Global constraints
- Agent illusion: no simulation/demo/paper/test/admin fingerprints in any agent-facing output (`test_mcp_no_fingerprints` stays green).
- Secrets never in logs, API responses, activity summaries, Telegram, MCP.
- Italian UI/messages, comma decimals; ruff clean; full backend + web suites green.
- No new paid dependency; HTTP via the existing httpx client and provider base classes.

## Task A · Market data
1. **Range-coverage fallback for bars**: when the first provider returns bars whose first bar starts materially after the requested `start` (more than 2 bar intervals + weekends/holidays tolerance for equities; 2 intervals for crypto) the router tries the next provider in priority and keeps the result with the widest coverage (ties → earlier provider). Cached result = the chosen one. Test: Kraken-like fake returning only the last 720 candles vs a full fake → full one wins; a provider covering the range is used without calling the next.
2. **Finnhub 403**: an HTTP 403 whose body says the resource/plan is not accessible maps to an «non incluso nel piano Finnhub» unsupported/unavailable error (provider skipped like unsupported, health stays ok), not «chiave non valida». 401 stays «chiave non valida».
## Task B · Crypto news (RSS)
New provider `rssnews` (connector name `rssnews`, label «Notizie crypto (CoinDesk, Cointelegraph)», no credentials, enabled by default): fetch https://www.coindesk.com/arc/outboundfeeds/rss/ and https://cointelegraph.com/rss, parse RSS 2.0 with the stdlib (defusedxml if already a dependency, else xml.etree with entity expansion disabled / size cap 2 MB), map to NewsItem (id = stable hash of link, ts from pubDate UTC, headline, summary stripped of HTML, url, source «CoinDesk»/«Cointelegraph»), filter by symbol: BTC-* → bitcoin|btc, ETH-* → ethereum|ether|eth, other crypto → base ticker and CoinGecko-style name if known; newest first, dedupe by link; capability news for crypto only. Migration 0029: connector row + append `rssnews` first in crypto/news priority (downgrade removes it). Health check = one feed fetch. Tests with recorded fixture XML (no network).
## Task C · Small fixes
1. **M2** `mcp/dto.py`: orders shown to agents never expose `source: "admin"` (map to the same value agent orders use, or drop the field consistently); admin routes unchanged.
2. **M1** Paperclip config: changing `base_url` host while keeping the stored board key requires `board_key` in the same request (422 with Italian message); same host keeps it.
3. **M4** `telegram/summaries._decision_section`: outer join on Agent, fallback name «Agente rimosso».
4. **M3** fingerprint filter (`mcp/dto._scrub` / FINGERPRINT): word-boundary matching so «testo», «simultaneo», «demolire», «attestato» pass; «test», «demo», «simulazione», «paper trading», «dry run», «sandbox» still caught. MCP sweep test stays strict.
5. **Flaky** `test_anonymous_failures_are_rate_limited_and_coalesced`: freeze the throttle clock in the test (no prod change).
## Review Focus
- A: bar fallback must not multiply provider calls for normal (covered) requests; intraday ranges; empty results.
- B: malformed/oversized feed, missing pubDate, HTML in description, duplicate links across feeds.
- C: M3 must not let a real fingerprint through in broker texts.
