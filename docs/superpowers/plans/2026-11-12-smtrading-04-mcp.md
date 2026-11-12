# SMTrading — Piano 4: Server MCP, agenti, audit, collegamento di Paperclip

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Esporre agli agenti AI (Paperclip) un server MCP Streamable HTTP su `http://api:8000/mcp`. Ogni agente ha il suo bearer token e vede solo i portafogli assegnati; tutti i metodi di portafoglio, ordini e dati di mercato della spec §9 sono disponibili; ogni chiamata è registrata in `mcp_audit_log`.

**Architecture:** `FastMCP` (SDK MCP ufficiale per Python, pacchetto `mcp`) in modalità `stateless_http=True` e `json_response=True`, montato nell'app FastAPI su `/mcp`. Un middleware ASGI valida il token e mette l'agente in una `ContextVar`. Ogni metodo passa da un unico runner (`run_tool`) che apre la sessione, controlla i permessi sul portafoglio, chiama il servizio di dominio, fa il commit, scrive l'audit in una transazione separata e trasforma gli `SMTError` in errori MCP `{"code","message","details"}`. Watchlist, alert di prezzo e backtest arrivano nel piano 6. Questo piano parte dal branch del piano 3.

**Tech Stack:** `mcp` (SDK Python, versione ≥ 1.10 con streamable HTTP), `argon2-cffi`, uvicorn (server reale nei test e2e).

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§9, §4 `agents`, `agent_portfolios`, `mcp_audit_log`, §11 collegamento di Paperclip)

## Global Constraints

- Valgono tutti i vincoli dei piani 1-3.
- Formato del token: `smt_<prefix:8 caratteri [a-z0-9]>_<secret:40 caratteri url-safe>`. Nel database si salvano `token_prefix` (in chiaro, UNIQUE) e `token_hash` (argon2id). Il token in chiaro viene mostrato **una sola volta**.
- Header: `Authorization: Bearer <token>`. Token mancante o non valido → HTTP 401 con corpo JSON `{"code":"FORBIDDEN","message":"invalid token"}`, prima che parta il protocollo MCP.
- Permessi: `read` per i metodi di lettura, `trade` per quelli che creano, modificano o annullano ordini. Un portafoglio non assegnato o un permesso insufficiente → `FORBIDDEN`.
- **Errori dei metodi:** l'MCP restituisce `isError: true` con un testo JSON `{"code": "...", "message": "...", "details": {...}}`. I codici sono quelli della spec §9.
- Nelle risposte i Decimal sono stringhe, i datetime sono in ISO 8601 UTC e gli UUID sono stringhe.
- Gli agenti **non** possono creare portafogli, depositare, prelevare o modificare i limiti: questi metodi non esistono nell'MCP.
- Nome del server MCP: `SMTrading`. URL interno: `http://api:8000/mcp`. Agente seed: `paperclip-demo`, con permesso `trade` sul portafoglio "Demo".

## Review Focus

1. **Agente che chiede un portafoglio di un altro agente** (per id, in qualsiasi metodo, compresi `get_order` e `cancel_order` per order_id) → `FORBIDDEN`, mai i dati. Test in Task 3 e Task 4.
2. **Token revocato o ruotato** → la chiamata successiva riceve 401 entro al massimo 60s dalla revoca (cache dei token verificati con TTL 60s), oppure subito se la revoca svuota la cache del processo. Test in Task 1 e Task 2.
3. **Eccezione inattesa in un metodo** (non `SMTError`) → errore MCP `{"code":"INTERNAL","message":"internal error"}` senza stack trace, con l'errore loggato e l'audit registrato con esito `INTERNAL`. Test in Task 2.
4. **Ordine rifiutato dal rischio** → l'ordine `rejected` è salvato (commit) e l'agente riceve l'errore con il codice, oltre all'audit. Test in Task 4.
5. **Parametri non validi** (decimal non parsabile, enum sconosciuto, UUID malformato) → `INVALID_ORDER` con un messaggio leggibile, nessun 500. Test in Task 2 e Task 4.

---

## File Structure

```
backend/src/smtrading/
  agents/__init__.py, models.py (Agent, AgentPortfolio, McpAuditLog), service.py
  mcp/__init__.py
  mcp/server.py        # build_mcp() -> FastMCP; registrazione di metodi e risorse
  mcp/auth.py          # McpAuthMiddleware, current_agent ContextVar
  mcp/runtime.py       # McpRuntime (sessionmaker, redis, quote source), run_tool, errori
  mcp/dto.py           # serializzazione di ordini, posizioni, valutazioni, journal, quote...
  mcp/tools_portfolio.py, tools_orders.py, tools_market.py, tools_alerts.py, resources.py
  mcp/smoke.py         # python -m smtrading.mcp.smoke --url ... --token ...
  performance/service.py   # get_performance (serie, rendimento, max drawdown, sharpe)
backend/migrations/versions/0005_agents_mcp.py
backend/tests/integration/test_agents.py, test_mcp_*.py, test_performance.py
docs/paperclip-mcp.md
scripts/connect-paperclip.sh
```

---

### Task 1: Agenti, token, permessi, audit (schema e servizio)

**Files:** Create `agents/{__init__,models,service}.py`, `migrations/versions/0005_agents_mcp.py`. Modify `migrations/env.py`, `seed.py`. Test: `tests/integration/test_agents.py`, `test_seed.py` (aggiunte).

**Interfaces — Produces:**
- `Agent`: `id, name UNIQUE, token_prefix UNIQUE, token_hash, active bool, created_at, last_seen_at|None`
- `AgentPortfolio`: PK `(agent_id, portfolio_id)`, `permission` ('read'|'trade')
- `McpAuditLog`: `id BIGINT identity, agent_id|None, method str, params JSONB, outcome str ('ok' oppure un codice di errore), latency_ms int, created_at`. Indice su `(agent_id, created_at)`.
- `def generate_token() -> tuple[str, str, str]`: restituisce (token, prefix, secret) con `secrets`.
- `async def create_agent(session, name: str) -> tuple[Agent, str]`: restituisce il token in chiaro. Nome duplicato → `INVALID_ORDER`.
- `async def rotate_token(session, agent_id) -> str`; `async def set_agent_active(session, agent_id, active: bool) -> Agent`
- `async def authenticate(session, token: str) -> Agent`:
  - token malformato, prefisso sconosciuto, hash diverso o agente inattivo → `SMTError(FORBIDDEN)`;
  - aggiorna `last_seen_at` al massimo una volta al minuto.
- `class TokenCache`: `get(token) -> UUID|None`, `put(token, agent_id)`, `invalidate_agent(agent_id)`, `clear()`. È in memoria, TTL 60s e chiave `sha256(token)`; `rotate_token` e `set_agent_active` chiamano `invalidate_agent` sull'istanza globale `token_cache`.
- `async def assign_portfolio(session, agent_id, portfolio_id, permission)` (upsert); `async def unassign_portfolio(...)`
- `async def get_permission(session, agent_id, portfolio_id) -> str|None`
- `async def require_permission(session, agent_id, portfolio_id, needed: Literal["read","trade"]) -> None`: con `trade` è implicito `read`; altrimenti `FORBIDDEN`.
- `async def agent_portfolio_ids(session, agent_id) -> list[UUID]`
- `async def write_audit(session, *, agent_id, method, params, outcome, latency_ms) -> None`
- `async def list_audit(session, *, agent_id=None, method=None, outcome=None, since=None, limit=200) -> list[McpAuditLog]`
- Seed: se non esiste l'agente `paperclip-demo` lo crea, gli assegna Demo con `trade` e stampa **una volta** su stdout `SMTrading: token agente paperclip-demo = <token> (salvalo: non verrà più mostrato)`.
- Test:
  - token: creazione e autenticazione; token sbagliato, prefisso sconosciuto e agente inattivo → FORBIDDEN;
  - dopo la rotazione il vecchio token non vale più;
  - permessi `read` e `trade`, compreso `trade` che implica `read`;
  - il seed crea l'agente una sola volta: seconda esecuzione → nessuna nuova stampa, verificabile con `capsys`;
  - il token salvato non è in chiaro;
  - `TokenCache` scade dopo il TTL (orologio iniettabile).
- [ ] Step: `uv add argon2-cffi` → FAIL → implementa → PASS → commit `feat(agents): agents, tokens, permissions, audit log`

---

### Task 2: Scheletro del server MCP, autenticazione e runner

**Files:** Create `mcp/{__init__,server,auth,runtime,dto}.py`, `mcp/tools_portfolio.py` (solo `list_portfolios` in questo task). Modify `api/main.py` (lifespan: crea `McpRuntime` con sessionmaker, redis e `MarketDataRouter` come quote source; monta `/mcp`; avvia il session manager di FastMCP nel lifespan). Test: `tests/integration/test_mcp_core.py`, più la fixture `mcp_server` in conftest.

**Interfaces:**
- `current_agent: ContextVar[UUID | None]`
- `class McpAuthMiddleware` (ASGI): solo per i path `/mcp*`. Legge `Authorization`; prima la cache, altrimenti `authenticate` con una sessione breve; in caso di errore risponde 401 con il JSON dei Global Constraints; in caso di successo imposta `current_agent`.
- `@dataclass class McpRuntime: sessionmaker; redis; qs: QuoteSource; router: MarketDataRouter | None`. È accessibile con `get_runtime()`, impostato nel lifespan e sovrascrivibile nei test.
- `async def run_tool(method: str, params: dict, fn: Callable[[AsyncSession, UUID], Awaitable[Any]], *, commit_on_error: bool = False) -> Any`:
  1. `agent_id = current_agent.get()`; se è None → FORBIDDEN.
  2. Apre la sessione, chiama `fn(session, agent_id)`, fa il commit.
  3. Gestione degli errori:
     - `SMTError` → rollback, ma con `commit_on_error=True` fa il commit (serve agli ordini rifiutati), poi solleva `ToolError(json.dumps(err.to_dict()))`;
     - `pydantic.ValidationError`, `ValueError`, `decimal.InvalidOperation` → `INVALID_ORDER`;
     - qualsiasi altra eccezione → log con stack e `ToolError('{"code":"INTERNAL","message":"internal error","details":{}}')`.
  4. Audit sempre, in una sessione separata, con `latency_ms` ed esito.
  5. Restituisce il risultato già serializzato con `dto.to_jsonable`.
- `dto.to_jsonable(obj) -> Any`: dataclass, ORM noti, Decimal, datetime, UUID, Enum, liste e dict. Sono previsti convertitori espliciti `order_dto`, `fill_dto`, `position_dto`, `valuation_dto`, `journal_dto`, `quote_dto`, `bar_dto`, `instrument_dto`, `notification_dto`, ognuno con un insieme di campi fisso e documentato.
- `def parse_decimal(v: str | int | float | None, field: str) -> Decimal | None`: un float viene convertito con `str()`; un valore non valido → `INVALID_ORDER` con `details.field`. `parse_uuid(v, field)` e `parse_enum(cls, v, field)` funzionano allo stesso modo.
- `build_mcp() -> FastMCP`: `FastMCP("SMTrading", stateless_http=True, json_response=True)`; registra metodi e risorse dai moduli `tools_*` con la funzione `register(mcp)` di ciascun modulo.
- Metodo `list_portfolios()`: `[{"id","name","base_currency","status","permission"}]`.
- Fixture `mcp_server` (in `tests/conftest.py`):
  - parte da un database `fresh_db`, Redis e `FakeQuoteSource`;
  - avvia uvicorn con `create_app()` in un thread o task su una porta libera, dopo aver impostato `get_runtime` per usare la `FakeQuoteSource`;
  - restituisce `(base_url, seed_token)`, con un agente e il portafoglio Demo creati dalla fixture;
  - fornisce anche un helper `mcp_call(url, token, tool, args) -> (is_error, payload)`, che usa `mcp.client.streamable_http.streamablehttp_client` + `ClientSession`.
- Test:
  - `list_tools` contiene `list_portfolios`;
  - token assente → la connessione fallisce con 401 (verificato con httpx diretto, `POST /mcp` → 401 e codice FORBIDDEN);
  - `list_portfolios` restituisce solo i portafogli assegnati;
  - un metodo di test registrato solo nel test, che solleva `RuntimeError`, → errore INTERNAL e audit `INTERNAL`;
  - un token revocato con `set_agent_active(False)` → 401 alla chiamata successiva (cache invalidata);
  - ogni chiamata produce una riga di audit.
- [ ] Step: `uv add mcp` → FAIL → implementa → PASS → commit `feat(mcp): server skeleton, bearer auth, tool runner, audit`

---

### Task 3: Metodi di portafoglio, performance e risorse

**Files:** Create `performance/service.py`, `mcp/resources.py`. Modify `mcp/tools_portfolio.py`. Test: `tests/integration/test_performance.py`, `test_mcp_portfolio.py`.

**Interfaces:**
- `async def get_performance(session, portfolio_id, period: Literal["1d","1w","1m","3m","1y","all"]) -> dict`:
  - `{"series": [{"ts","equity_eur"}], "return_pct", "max_drawdown_pct", "sharpe"}`, calcolato dagli `equity_snapshots`;
  - `return_pct = (ultimo/primo − 1) × 100`;
  - drawdown massimo sulla serie;
  - sharpe = media dei rendimenti giornalieri (da `day_open`) / deviazione standard × √252, oppure None se ci sono meno di 3 punti;
  - Decimal con 4 decimali.
- Metodi:
  - `get_portfolio(portfolio_id)` → `valuation_dto(value_portfolio(...))`, più `status`, `reduce_only_reason`, `leverage`, P&L del giorno (equity − day_open) in EUR e %;
  - `get_positions(portfolio_id)` → le `PositionValuation` serializzate, lato long o short;
  - `get_transactions(portfolio_id, from_ts=None, to_ts=None, type=None, limit=100)` → journal con solo le righe degli account del portafoglio: `{id, seq, type, created_at, reason, order_id, lines:[{asset, kind, quantity, unit_price, price_ccy}]}`, in ordine `seq` decrescente;
  - `get_performance(portfolio_id, period="1m")`;
  - `get_risk_limits(portfolio_id)` → profilo, leva, soglie, margine attuale (`equity`, `maint_req`, `warn_req`, `buying_power`), `distance_to_maintenance_eur`, perdita giornaliera attuale e soglia, kill-switch.
- Tutti richiedono `read` su quel portafoglio.
- Risorse:
  - `portfolio://{portfolio_id}`, uguale a `get_portfolio`;
  - `fees://schedule`, con le regole di commissione attive applicabili ai portafogli dell'agente (per ogni portafoglio la regola che vale per ogni classe di asset e lato, risolta con `resolve_rule`);
  - `markets://hours`, cioè `all_market_status(now)`.
- Test:
  - valori esatti di `get_portfolio` su un portafoglio con posizioni note (FakeQuoteSource);
  - le transazioni contengono solo le righe del portafoglio;
  - performance su snapshot sintetici (rendimento +10%, drawdown −5%);
  - FORBIDDEN su un portafoglio non assegnato per **ogni** metodo, con un test parametrizzato;
  - le tre risorse si leggono con `read_resource`.
- [ ] Step: FAIL → implementa → PASS → commit `feat(mcp): portfolio tools, performance, resources`

---

### Task 4: Metodi degli ordini

**Files:** Create `mcp/tools_orders.py`. Test: `tests/integration/test_mcp_orders.py`.

**Interfaces:** ogni metodo richiede `trade` (tranne `get_order`, `list_orders` e `preview_order`, che richiedono `read`) e fa il controllo sul portafoglio dell'ordine anche quando riceve solo `order_id`.
- `preview_order(portfolio_id, symbol, side, type, qty?, notional?, limit_price?, stop_price?, trail_pct?, trail_amount?)` → `CheckResult` serializzato: con `ok=false` il risultato contiene il codice, ma **non** è un errore MCP.
- `place_order(... come preview ..., tif="gtc", client_order_id?)` → `order_dto`. Usa `run_tool(..., commit_on_error=True)` per salvare gli ordini rifiutati. Per `source` e `agent_id` usa l'agente corrente.
- `place_bracket_order(portfolio_id, symbol, side, qty, type="market", limit_price?, take_profit, stop_loss, tif="gtc", client_order_id?)` → lista di ordini
- `place_oco_order(portfolio_id, symbol, side, qty, a: {type, limit_price?, stop_price?}, b: {...}, tif="gtc")` → lista
- `modify_order(order_id, qty?, limit_price?, stop_price?, trail_pct?, trail_amount?)`, `cancel_order(order_id)`, `cancel_all_orders(portfolio_id, symbol?)` → `{"canceled": n}`
- `get_order(order_id)` → `order_dto` con `fills`; `list_orders(portfolio_id, status?: list[str], limit=100)`
- `close_position(portfolio_id, symbol, qty?)`, `close_all_positions(portfolio_id)`
- `order_dto`: `id, client_order_id, portfolio_id, symbol, side, type, tif, qty, notional, limit_price, stop_price, trail_pct, trail_amount, status, filled_qty, avg_fill_price, parent_id, oco_group_id, role, reject_code, reject_message, created_at, updated_at`.
- Test, sul server reale della fixture:
  - market buy eseguito e cash aggiornato in `get_portfolio`;
  - limit `pending` e poi `cancel`;
  - ordine rifiutato (`qty` enorme → `INSUFFICIENT_BUYING_POWER`): l'errore MCP ha quel codice e `list_orders(status=["rejected"])` lo contiene;
  - `client_order_id` idempotente;
  - un agente con solo `read` → FORBIDDEN su `place_order`;
  - `cancel_order` di un ordine di un altro portafoglio → FORBIDDEN;
  - `qty="abc"` → INVALID_ORDER;
  - bracket e OCO creati;
  - `close_position`.
- [ ] Step: FAIL → implementa → PASS → commit `feat(mcp): order tools`

---

### Task 5: Metodi dei dati di mercato

**Files:** Create `mcp/tools_market.py`. Test: `tests/integration/test_mcp_market.py` (con `FakeQuoteSource` estesa: aggiungi `set_bars`, `set_news`, `set_fundamentals`, `set_search`, `set_screen` e i metodi corrispondenti; il protocollo `QuoteSource` resta minimo, mentre questi metodi passano da `McpRuntime.router`, che nei test è un `FakeMarketData` con la stessa interfaccia di `MarketDataRouter` per `get_bars`, `get_news`, `get_fundamentals`, `get_calendar`, `search`, `screen`).

**Interfaces:** tutti i metodi richiedono un agente valido ma nessun portafoglio.
- `search_instruments(query, asset_class?, limit=20)`: prima l'anagrafica locale (`instruments` con ILIKE su simbolo o nome), poi il router (`search`); unione senza duplicati con il campo `tradable` (true solo per i simboli presenti in `instruments` e attivi).
- `get_instrument(symbol)` → anagrafica, più `market_status`.
- `get_quote(symbols: list[str])` → `{symbol: quote_dto | {"error": code}}`; massimo 50 simboli. Prima tenta il batch `router.get_quotes(symbols)`; se fallisce, richiede i simboli uno alla volta in parallelo per isolare gli errori.
- `get_bars(symbol, timeframe, from_ts?, to_ts?, limit=500)`: per default l'intervallo arriva fino ad adesso, a ritroso in base al timeframe.
- `get_market_status(market?)`
- `get_indicators(symbol, timeframe, indicators: list[{name, params}], lookback=100)`
- `get_fx_rate(base, quote)`
- `get_news(symbols?, limit=20)`, `get_fundamentals(symbol)`, `get_earnings_calendar(from_date, to_date, symbols?)`
- `get_dividends(symbol, from_date?, to_date?)`: dal calendario, kind = dividend
- `screen(asset_class?, sector?, min_market_cap?, max_market_cap?, min_change_pct?, max_change_pct?, min_volume?, max_pe?, limit=25)`
- Test: ogni metodo con i dati finti; `get_quote` con un simbolo che fallisce restituisce l'errore per quel simbolo senza far fallire gli altri; un timeframe non valido → INVALID_ORDER; il limite di 50 simboli.
- [ ] Step: FAIL → implementa → PASS → commit `feat(mcp): market data tools`

---

### Task 6: Notifiche, smoke CLI, Docker, documentazione per Paperclip

**Files:** Create `mcp/tools_alerts.py` (solo `get_triggered_alerts`; il resto degli alert arriva nel piano 6), `mcp/smoke.py`, `docs/paperclip-mcp.md`, `scripts/connect-paperclip.sh`. Test: `tests/integration/test_mcp_e2e.py`.

**Interfaces:**
- `get_triggered_alerts(since?: datetime, limit=100)` → le `notifications` dei portafogli dell'agente oppure con `agent_id` = l'agente, dalla più recente, filtrate per `created_at > since`.
- `mcp/smoke.py`: `python -m smtrading.mcp.smoke --url http://localhost:8000/mcp --token <t>`. Si collega, stampa il numero di metodi e chiama `list_portfolios`, `get_quote(["BTC-USD"])` e `get_market_status()`. Exit code 0 se va tutto bene.
- `scripts/connect-paperclip.sh`: `docker network connect smtrading-net "${1:-paperclip}"` con un controllo idempotente (se è già collegato, non fa nulla). **Non va eseguito** in questo piano: lo esegue l'utente.
- `docs/paperclip-mcp.md` (in italiano):
  1. come recuperare il token (log del primo avvio, oppure rotazione dalla web app nel piano 5);
  2. lanciare `scripts/connect-paperclip.sh`;
  3. configurare in Paperclip un server MCP HTTP con URL `http://api:8000/mcp` (oppure `http://smtrading-api-1:8000/mcp`) e header `Authorization: Bearer <token>`;
  4. un esempio JSON di configurazione MCP (`{"mcpServers":{"smtrading":{"type":"http","url":"http://api:8000/mcp","headers":{"Authorization":"Bearer ..."}}}}`);
  5. l'elenco dei metodi con una riga di descrizione ciascuno;
  6. i codici di errore.
- Test e2e: `list_tools` contiene **tutti** i nomi dei metodi della spec §9 implementati in questo piano (un elenco fisso nel test); poi un flusso completo: `list_portfolios` → `get_quote` → `preview_order` → `place_order` → `get_positions` → `get_transactions` → `close_position` → `get_triggered_alerts`.
- **Verifica in Docker:** `docker compose up -d --build`; il token viene da `docker compose logs api | grep "token agente"`; `docker compose exec api python -m smtrading.mcp.smoke --url http://localhost:8000/mcp --token <t>` → exit 0. Inoltre `curl -s -o /dev/null -w "%{http_code}" -X POST localhost:8000/mcp` → 401.
- [ ] Step: FAIL → implementa → PASS → verifica in Docker → commit `feat(mcp): alerts tool, smoke client, paperclip docs`
