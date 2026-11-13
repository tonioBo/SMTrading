# SMTrading — Piano 6: Connettori con chiave, broker paper, watchlist e alert, backtest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- Completare i fornitori di dati con chiave API: Alpaca Market Data, Finnhub, Twelve Data e Revolut X (solo dati).
- Replicare gli ordini sui broker paper (Alpaca Paper, Trading 212 Demo), con riconciliazione e pagina Broker nell'admin.
- Aggiungere watchlist e alert di prezzo per gli agenti.
- Aggiungere il backtest con strategie predefinite.
- Esporre tutto tramite MCP.

**Architecture:**
- **Fornitori:** nuove sottoclassi di `DataProvider` (piano 2), registrate in `PROVIDERS`. Senza chiave restano `enabled=false`; nei test usano solo respx e WebSocket locali.
- **Broker:**
  - interfaccia `BrokerAdapter`; `engine.service.place_order` inoltra al broker gli ordini dei portafogli collegati, se il broker supporta tipo e strumento;
  - il `BrokerSync` nel worker interroga ogni 2s gli ordini aperti sul broker e registra gli eseguiti con `execute_fill(..., fill_qty=…, fill_price=…, source="broker")`;
  - la fonte di verità resta il nostro registro.
- **Alert:** valutati a ogni tick dall'`AlertEvaluator`, salvati in `notifications` (letti da `get_triggered_alerts` del piano 4).
- **Backtest:** motore puro su barre storiche; un runner nel worker processa la coda della tabella `backtests`.
- Questo piano parte dal branch del piano 5.

**Tech Stack:** come prima. In più: `cryptography` (Ed25519 per Revolut X), dnd-kit (già presente dal piano 5).

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§8 connettori e `BrokerAdapter`, §5 punto 9, §9 watchlist, alert e backtest, §10 pagina Broker)

## Global Constraints

- Valgono i vincoli dei piani 1-5.
- **Configurazione dei connettori** (cifrata, chiavi in `config`):

  | Connettore | Chiavi |
  |---|---|
  | alpaca | `key_id`, `secret_key`, `feed` (default `"iex"`) |
  | finnhub | `api_key` |
  | twelvedata | `api_key` |
  | revolutx | `api_key`, `private_key_pem` (Ed25519) |

- **Configurazione dei broker** (`broker_links`, cifrata):
  - `alpaca_paper`: `key_id`, `secret_key`, base URL fissa `https://paper-api.alpaca.markets`;
  - `t212_demo`: `api_key` e, se l'API in uso lo richiede, `api_secret`; base URL fissa `https://demo.trading212.com`.
- **Mai URL di produzione dei broker:** gli adapter rifiutano in costruzione qualsiasi `base_url` diversa da quella paper o demo e sollevano `SMTError(FORBIDDEN)`. Revolut X non ha un adapter broker.
- **Documentazione delle API esterne:** prima di implementare ogni fornitore o broker, l'implementer consulta la documentazione ufficiale attuale (WebFetch) e registra nel report gli URL e i formati verificati. Se un formato differisce da questo piano, vale la documentazione, con la decisione annotata nel report.
- **Backtest:** solo long; esecuzione all'apertura della barra successiva al segnale; slippage e commissioni del portafoglio indicato, oppure della regola globale se non c'è un portafoglio; nessuna leva.
- **Limiti per agente:** al massimo 20 watchlist, 200 simboli per watchlist e 100 alert attivi; al massimo 2 backtest in esecuzione contemporaneamente per agente, oltre → `RATE_LIMITED`.

## Review Focus

1. **Broker paper che rifiuta l'ordine** (es. il simbolo non è negoziabile su T212) → l'ordine diventa `rejected` con `reject_code=PROVIDER_UNAVAILABLE` e il messaggio del broker; nessun journal; notifica all'agente. Test in Task 8.
2. **Esecuzione parziale sul broker** → due eseguiti successivi che producono due journal TRADE; stato `partially_filled` e poi `filled`; `filled_qty` e `avg_fill_price` corretti. Test in Task 8.
3. **Chiave API errata su un connettore con chiave** → il test di connessione risponde `{ok:false, error}` e il router passa al fornitore successivo senza bloccarsi. Test in Task 1.
4. **Alert su un simbolo senza stream** → valutato anche dallo sweep in polling, non solo sui tick. Test in Task 10.
5. **Backtest su un periodo senza barre o con meno barre del periodo della strategia** → risultato `completed` con 0 trade e l'avviso `insufficient_data`, non `failed`. Test in Task 11.

---

## File Structure

```
backend/src/smtrading/
  marketdata/providers/{alpaca,finnhub,twelvedata,revolutx}.py
  brokers/__init__.py, models.py (BrokerLink, Reconciliation), base.py (BrokerAdapter, BrokerOrder, BrokerFill),
    alpaca_paper.py, t212_demo.py, registry.py, service.py (link CRUD), sync.py (BrokerSync, reconcile)
  watch/__init__.py, models.py (Watchlist, WatchlistItem, Alert), service.py, evaluator.py (AlertEvaluator)
  backtest/__init__.py, models.py (Backtest), strategies.py, engine.py (puro), runner.py
  mcp/tools_watch.py, mcp/tools_backtest.py
  admin/routes_brokers.py
backend/migrations/versions/0008_brokers_watch_backtest.py
web/src/pages/Brokers.tsx (+ voce di menu "Broker")
backend/tests/unit/providers/test_{alpaca,finnhub,twelvedata,revolutx}.py
backend/tests/unit/test_backtest_engine.py
backend/tests/integration/test_brokers_*.py, test_watch.py, test_backtest_runner.py, test_mcp_watch_backtest.py
```

---

### Task 1: Fornitore Alpaca Market Data (REST + WS)

**Files:** `marketdata/providers/alpaca.py`, fixture `tests/fixtures/marketdata/alpaca_*.json`; `registry.py`. Test: `tests/unit/providers/test_alpaca.py`.

**Interfaces:**
- `AlpacaProvider`: `name="alpaca"`, capacità `{quote, bars, stream, news}`, asset `{us_equity, crypto}`, chiave richiesta, 200 chiamate al minuto.
- Header: `APCA-API-KEY-ID` e `APCA-API-SECRET-KEY`.
- Endpoint REST, base `https://data.alpaca.markets`:
  - azioni: `/v2/stocks/quotes/latest?symbols=&feed=` (bid `bp`, ask `ap`) e `/v2/stocks/trades/latest` (last `p`);
  - barre: `/v2/stocks/bars?symbols=&timeframe=1Min|5Min|15Min|1Hour|1Day|1Week&start=&end=&feed=`;
  - crypto: `/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD` e `/v1beta3/crypto/us/bars`;
  - news: `/v1beta1/news?symbols=&limit=`.
- WebSocket:
  - azioni: `wss://stream.data.alpaca.markets/v2/{feed}`, autenticazione con `{"action":"auth","key","secret"}`, poi `{"action":"subscribe","quotes":[...]}`, messaggi `T=="q"`;
  - crypto: `wss://stream.data.alpaca.markets/v1beta3/crypto/us`.
- `map_symbol`: azioni invariate; `BTC-USD → BTC/USD`; eu_equity e forex → `ProviderUnsupported`.
- `health_check`: GET delle quote latest per `SPY`; 401 o 403 → eccezione con il messaggio "chiave non valida".
- Test:
  - parsing da fixture: prendi gli esempi dalla documentazione ufficiale, perché senza chiave non si possono registrare risposte reali; annota nel report da dove arriva ogni fixture;
  - 401 → eccezione, e con il router a fallback (fornitore finto come secondo) si ottiene la quota dal secondo (Review Focus 3);
  - parser WS puro `parse_alpaca_ws(msg_list)`;
  - stream con un server WS locale: autenticazione, sottoscrizione, 2 quote.
- [ ] FAIL → implementa → PASS → commit `feat(marketdata): alpaca market data provider`

### Task 2: Fornitore Finnhub (REST + WS)

**Files:** `marketdata/providers/finnhub.py`, fixture; `registry.py`. Test: `test_finnhub.py`.

**Interfaces:**
- `name="finnhub"`, capacità `{quote, stream, news, fundamentals, calendar}`, asset `{us_equity, eu_equity}`, chiave richiesta, 60 chiamate al minuto.
- Base `https://finnhub.io/api/v1`, parametro `token`.
- Endpoint:
  - `/quote?symbol=` → `c` (last), con bid/ask None;
  - `/company-news?symbol=&from=&to=`;
  - `/stock/profile2?symbol=` e `/stock/metric?symbol=&metric=all` → `Fundamentals`;
  - `/calendar/earnings?from=&to=&symbol=`;
  - `/stock/dividend` se il piano lo permette; altrimenti il calendario dei dividendi resta su Yahoo.
- WS: `wss://ws.finnhub.io?token=`, messaggi `{"type":"subscribe","symbol":"AAPL"}`, eventi `type=="trade"` → `Quote(last=p)`.
- `map_symbol`: us invariato; eu: `ENI.MI → ENI.MI`. Verifica il formato nella documentazione: Finnhub usa il suffisso dell'exchange.
- Test: come nel Task 1 (fixture, parser WS, stream locale, 401).
- [ ] FAIL → implementa → PASS → commit `feat(marketdata): finnhub provider`

### Task 3: Fornitore Twelve Data (REST + WS)

**Files:** `marketdata/providers/twelvedata.py`, fixture; `registry.py`. Test: `test_twelvedata.py`.

**Interfaces:**
- `name="twelvedata"`, capacità `{quote, bars, stream, fx}`, asset `{eu_equity, forex, us_equity}`, chiave richiesta, 8 chiamate al minuto (piano gratuito).
- Base `https://api.twelvedata.com`, parametro `apikey`.
- Endpoint:
  - `/quote?symbol=` (bid e ask non garantiti; `close` come last);
  - `/time_series?symbol=&interval=1min|5min|15min|1h|1day|1week&start_date=&end_date=`;
  - `/exchange_rate?symbol=EUR/USD`.
- WS: `wss://ws.twelvedata.com/v1/quotes/price?apikey=`, `{"action":"subscribe","params":{"symbols":"EUR/USD,ENI"}}`, eventi `event=="price"`.
- `map_symbol`: `EURUSD → EUR/USD`; `ENI.MI → ENI:MTA` e `SAP.DE → SAP:XETR`, a seconda del formato documentato per l'exchange (verifica e annota); us invariato.
- Errori: Twelve Data risponde HTTP 200 con `{"status":"error","code":...}` → eccezione.
- Test: come nel Task 1, più l'errore con HTTP 200.
- [ ] FAIL → implementa → PASS → commit `feat(marketdata): twelve data provider`

### Task 4: Revolut X (solo dati)

**Files:** `marketdata/providers/revolutx.py`; `registry.py`. Test: `test_revolutx.py`.

**Interfaces:**
- `name="revolutx"`, capacità `{quote}`, asset `{crypto}`, chiave richiesta, 60 chiamate al minuto.
- Usa l'endpoint documentato delle quote o del ticker per coppia, verificandolo nella documentazione ufficiale (developer.revolut.com, Revolut X Crypto Exchange API).
- La firma va calcolata come prevede la documentazione: Ed25519 su `timestamp + metodo + path + query + body`, con gli header `X-Revx-API-Key`, `X-Revx-Timestamp` e `X-Revx-Signature`. Verifica nella documentazione e adatta se serve.
- Funzione pura `sign_request(private_key_pem, timestamp_ms, method, path, query, body) -> str` testata con una chiave generata nel test e verificata con la chiave pubblica.
- `map_symbol`: `BTC-USD → BTC-USD` oppure `BTC/USD`, secondo la documentazione.
- **Non si implementa nessun endpoint di ordini.**
- Test: firma verificabile; parsing di una fixture costruita dalla documentazione; `ProviderUnsupported` per i simboli non crypto.
- [ ] FAIL → implementa → PASS → commit `feat(marketdata): revolut x data-only provider`

### Task 5: Schema per broker, watchlist, alert e backtest

**Files:** `migrations/versions/0008_brokers_watch_backtest.py`, `brokers/models.py`, `watch/models.py`, `backtest/models.py`; modifiche a `engine/models.py` (stato `partially_filled`, campi broker) e `portfolios/models.py` (`broker_link_id`). Test: `test_migrations.py` (drift).

**Interfaces — Produces:**
- `BrokerLink`: `id, name UNIQUE, kind ('alpaca_paper'|'t212_demo'), config_encrypted, enabled, status ('ok'|'error'|'unknown'), last_error, created_at`.
- `Portfolio.broker_link_id FK|None`.
- Su `Order` si aggiungono `broker_link_id|None`, `broker_status str|None`, `broker_replicated bool default false`, `broker_last_sync_at|None`; `OrderStatus` acquisisce `partially_filled`, che conta come stato aperto.
- `Reconciliation`: `id, broker_link_id, portfolio_id, ts, diffs JSONB [{symbol, ours, broker}], ok bool`.
- `Watchlist`: `id, agent_id FK, name, created_at`, UNIQUE(agent_id, name).
- `WatchlistItem`: PK `(watchlist_id, symbol)`.
- `AlertCondition(StrEnum)`: `price_above, price_below, change_pct_above, change_pct_below`. La variazione % è calcolata rispetto a `ref_price`, cioè la quota al momento della creazione.
- `Alert`: `id, agent_id FK, symbol, condition, value NUMERIC, ref_price NUMERIC|None, status ('active'|'triggered'|'deleted'), created_at, triggered_at|None, triggered_price|None, note str|None`.
- `BacktestStatus(StrEnum)`: `queued, running, completed, failed`.
- `Backtest`: `id, agent_id|None, strategy str, symbols JSONB, timeframe, start, end, initial_cash NUMERIC, params JSONB, portfolio_id|None, status, warnings JSONB, result JSONB|None, error|None, created_at, started_at, finished_at`.
- Test: tabelle presenti e `test_migration_matches_models` verde.
- [ ] FAIL → implementa → PASS → commit `feat: schema for brokers, watchlists, alerts, backtests`

### Task 6: BrokerAdapter e Alpaca Paper

**Files:** `brokers/{base,alpaca_paper,registry,service}.py`. Test: `tests/integration/test_brokers_alpaca.py` (con respx).

**Interfaces:**
- `@dataclass BrokerOrder: broker_order_id: str; status: Literal["accepted","partially_filled","filled","canceled","rejected","expired"]; filled_qty: Decimal; avg_fill_price: Decimal|None; message: str|None; raw: dict`
- `class BrokerAdapter`:
  - `kind: ClassVar[str]`, `supported_types: ClassVar[frozenset[OrderType]]`, `supported_asset_classes: ClassVar[frozenset[AssetClass]]`
  - `__init__(config: dict, http)`
  - async: `place_order(order: Order, instrument: Instrument) -> BrokerOrder`, `get_order(broker_order_id) -> BrokerOrder`, `cancel_order(broker_order_id) -> None`, `get_positions() -> dict[str, Decimal]` (simbolo canonico → qty con segno), `health_check() -> None`
  - `def supports(self, order: Order, instrument: Instrument) -> bool`
- `AlpacaPaperAdapter`:
  - tipi supportati: tutti e 5; asset `{us_equity, crypto}`;
  - `POST /v2/orders` con `{symbol, qty | notional, side, type, time_in_force: day|gtc, limit_price, stop_price, trail_percent | trail_price, client_order_id: str(order.id)}`; crypto con il simbolo `BTC/USD` e `time_in_force` `gtc`;
  - `GET /v2/orders/{id}`, `DELETE /v2/orders/{id}`, `GET /v2/positions`;
  - conversione degli stati Alpaca → `BrokerOrder.status`: new, accepted e pending_new → accepted; e così via.
- `brokers/service.py`:
  - `async create_link(session, name, kind, config) -> BrokerLink`, `update_link`, `list_links`;
  - `async get_adapter(session, link_id, http) -> BrokerAdapter`;
  - `async link_portfolio(session, portfolio_id, link_id|None)`.
- Test (respx):
  - payload di `place_order` per market, limit e trailing;
  - conversione degli stati;
  - 422 → `BrokerOrder(status="rejected", message=…)`, non un'eccezione;
  - una `base_url` diversa da quella paper → FORBIDDEN in costruzione;
  - `get_positions` restituisce i simboli canonici.
- [ ] FAIL → implementa → PASS → commit `feat(brokers): adapter interface and alpaca paper`

### Task 7: Adapter Trading 212 Demo

**Files:** `brokers/t212_demo.py`; `registry.py`. Test: `tests/integration/test_brokers_t212.py`.

**Interfaces:**
- Tipi supportati: `{market, limit, stop, stop_limit}` (niente trailing); asset `{us_equity, eu_equity}`.
- Endpoint, da verificare nella documentazione pubblica attuale (`https://t212public-api-docs.redoc.ly/` o quella aggiornata):
  - `POST /api/v0/equity/orders/market {ticker, quantity}`, con quantità negativa per i sell;
  - `/limit {ticker, quantity, limitPrice, timeValidity: DAY|GOOD_TILL_CANCEL}`, `/stop`, `/stop_limit`;
  - `GET /api/v0/equity/orders/{id}`, `DELETE /api/v0/equity/orders/{id}`, `GET /api/v0/equity/portfolio`;
  - autenticazione nel formato documentato (header `Authorization` con la chiave, oppure Basic key:secret).
- Mappatura dei ticker: `AAPL → AAPL_US_EQ`; per gli europei si usa una tabella dei ticker dello strumento (endpoint `/api/v0/equity/metadata/instruments`, in cache per 24h) che cerca per ISIN o per simbolo base e suffisso. Se non trova nulla → `supports()` restituisce False, e l'ordine resta solo simulato.
- Test (respx): payload per ogni tipo; quantità negativa per il sell; conversione degli stati; `supports` False per trailing e crypto; FORBIDDEN con un URL diverso dalla demo.
- [ ] FAIL → implementa → PASS → commit `feat(brokers): trading 212 demo adapter`

### Task 8: Instradamento al broker, BrokerSync, riconciliazione

**Files:** `brokers/sync.py`; modifiche a `engine/service.py` (`place_order`, `cancel_order`, `modify_order`), `engine/execution.py` (override di quantità e prezzo, eseguiti parziali), `worker/main.py`, `jobs/scheduler.py`. Test: `tests/integration/test_brokers_sync.py` (con un `FakeBrokerAdapter` registrato nel test).

**Interfaces:**
- `execute_fill(..., fill_qty: Decimal|None = None, fill_price: Decimal|None = None, source: Literal["sim","broker"] = "sim")`:
  - con gli override si usano quantità e prezzo dati (senza slippage);
  - con `filled_qty < qty` → stato `partially_filled`; `avg_fill_price` è la media ponderata;
  - l'OCO e i bracket si attivano solo con `filled`.
- In `place_order`, dopo `pre_trade_check` e con il mercato aperto: se il portafoglio ha un `broker_link_id` attivo e `adapter.supports(order, instr)` → `adapter.place_order`:
  - risposta `rejected` → ordine `rejected` con `PROVIDER_UNAVAILABLE` e il messaggio, più `notify`;
  - altrimenti `pending` con `broker_replicated=True` e `broker_order_id`; nessuna esecuzione locale;
  - se il broker non supporta l'ordine → flusso simulato normale, con `broker_replicated=False`.
  - Ruling: a mercato chiuso, l'ordine `queued` viene inviato al broker dallo sweep all'apertura, con la stessa logica.
- `cancel_order` e `modify_order` per gli ordini replicati: annullamento sul broker (e, per la modifica, annullamento più nuovo invio, visto che la modifica nativa è facoltativa); un errore del broker → `PROVIDER_UNAVAILABLE`.
- `class BrokerSync`: `run()`, ogni 2s:
  - per ogni ordine aperto con `broker_replicated`, chiama `get_order`;
  - se `filled_qty` è aumentata, registra un eseguito con `execute_fill(fill_qty=delta, fill_price=prezzo_delta, source="broker")`, dove `prezzo_delta = (avg_new × filled_new − avg_old × filled_old) / delta`;
  - gli stati finali `canceled`, `rejected` ed `expired` vengono applicati all'ordine;
  - ogni ordine ha la sua transazione.
- `async def reconcile(session, link_id, http) -> list[Reconciliation]`: per ogni portafoglio collegato confronta `get_positions()` con i saldi delle nostre posizioni e salva un `Reconciliation`. Lo scheduler lo esegue ogni 5 minuti.
- Il **trailing stop** sui broker che non lo supportano resta simulato.
- Test:
  - ordine instradato → nessun journal → `get_order` finto restituisce 5 di 10 eseguiti → un journal e stato `partially_filled` → poi 10 di 10 → secondo journal e stato `filled`, con l'avg corretto (Review Focus 2);
  - broker che rifiuta → `rejected` e notifica (Review Focus 1);
  - trailing su un broker senza trailing → simulato;
  - `cancel` propagato;
  - la riconciliazione rileva una differenza.
- [ ] FAIL → implementa → PASS → commit `feat(brokers): order routing, broker sync, reconciliation`

### Task 9: API admin e pagina Broker

**Files:** `admin/routes_brokers.py`, `web/src/pages/Brokers.tsx`, `web/src/api/hooks/brokers.ts`, voce di menu "Broker" in `Sidebar`. Test: `tests/integration/test_admin_brokers.py`, `web/src/pages/Brokers.test.tsx`.

**Interfaces:**
- `GET/POST /api/broker-links`, `PATCH /api/broker-links/{id}` (configurazione mascherata come per i connettori);
- `POST /api/broker-links/{id}/test`;
- `PUT /api/portfolios/{id}/broker-link {broker_link_id|null}`;
- `GET /api/broker-links/{id}/reconciliations?limit=`, `POST /api/broker-links/{id}/reconcile`.
- Pagina: card per collegamento (tipo, stato, test, chiavi mascherate); assegnazione ai portafogli; tabella delle riconciliazioni con le differenze evidenziate; nota "Solo conti paper/demo: nessun denaro reale".
- Nella tab Ordini del dettaglio portafoglio si aggiunge il badge "Broker" per `broker_replicated` e "Solo simulato" per gli ordini di un portafoglio collegato che non sono replicati.
- Test: CRUD con configurazione mascherata; collegamento a un portafoglio; render della pagina con una differenza evidenziata.
- [ ] FAIL → implementa → PASS → commit `feat(admin): broker links API and page`

### Task 10: Watchlist e alert (servizi, valutatore, MCP)

**Files:** `watch/{service,evaluator}.py`, `mcp/tools_watch.py`; `worker/main.py`. Test: `tests/integration/test_watch.py`, `test_mcp_watch_backtest.py` (parte watch).

**Interfaces:**
- Watchlist:
  - `create_watchlist(session, agent_id, name)`, `add_to_watchlist(session, agent_id, watchlist_id, symbols)`, `remove_from_watchlist`, `list_watchlists(session, agent_id)`, `delete_watchlist`;
  - `add` chiama `track(source="watchlists")`; quando un simbolo non è più in nessuna watchlist → `untrack`.
- Alert:
  - `create_alert(session, qs, agent_id, symbol, condition, value, note=None)`: `ref_price` = quota attuale per le condizioni `change_pct_*`; chiama `track(source="alerts")`;
  - `list_alerts(session, agent_id, status=None)`, `delete_alert(session, agent_id, alert_id)`: un alert di un altro agente → `FORBIDDEN`.
- Si applicano i limiti dei Global Constraints → `RISK_LIMIT_EXCEEDED`.
- `class AlertEvaluator`:
  - `on_tick(symbol, quote)` e `sweep()` (ogni 5s, con `qs.get_quotes` per i simboli con alert attivi);
  - prezzo di riferimento: `last`, altrimenti il mid;
  - quando un alert scatta: `status=triggered`, `triggered_at`, `triggered_price`, più `notify(kind="price_alert", agent_id=…, payload={alert_id, symbol, condition, value, price})`;
  - un alert scatta una sola volta.
- Metodi MCP: `list_watchlists`, `create_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `create_alert`, `list_alerts`, `delete_alert`.
- Test:
  - un alert `price_above` scatta con un tick e compare in `get_triggered_alerts`;
  - `change_pct_below` −5% calcolato da `ref_price`;
  - un simbolo senza tick scatta tramite lo sweep (Review Focus 4);
  - un alert di un altro agente → FORBIDDEN;
  - limite di 100 alert;
  - `add_to_watchlist` segue il simbolo.
- [ ] FAIL → implementa → PASS → commit `feat(watch): watchlists, price alerts, evaluator, MCP tools`

### Task 11: Backtest (motore puro, runner, MCP)

**Files:** `backtest/{strategies,engine,runner}.py`, `mcp/tools_backtest.py`; `worker/main.py`. Test: `tests/unit/test_backtest_engine.py`, `tests/integration/test_backtest_runner.py`, `test_mcp_watch_backtest.py` (parte backtest).

**Interfaces:**
- `strategies.py`, segnali puri `def signals(bars: list[Bar], params) -> list[Literal["buy","sell","hold"]]`:
  - `sma_cross(fast=20, slow=50)`: buy quando la SMA veloce incrocia al rialzo la lenta, sell quando la incrocia al ribasso;
  - `rsi_reversion(period=14, buy_below=30, sell_above=70)`;
  - `breakout(lookback=20, exit_lookback=10)`: buy quando `close > max(high` delle `lookback` barre precedenti`)`, sell quando `close < min(low` delle `exit_lookback` barre precedenti`)`;
  - `buy_and_hold()`: buy sulla prima barra, nessun sell.
  - Riusa gli indicatori del piano 2. Parametri sconosciuti o non validi (fast ≥ slow) → `INVALID_ORDER`.
- `engine.py`: `def run_backtest(bars_by_symbol: dict[str, list[Bar]], strategy: str, params: dict, *, initial_cash: Decimal, fee_rule: FeeRuleData, slippage_bps: int, lot_by_symbol: dict[str, Decimal]) -> dict`:
  - il cash è diviso in parti uguali tra i simboli;
  - ogni segnale viene eseguito all'**apertura** della barra successiva con lo slippage, e la quantità è il massimo intero di lotti acquistabili con il cash disponibile al netto della commissione;
  - al termine le posizioni aperte vengono valutate alla chiusura dell'ultima barra;
  - risultato: `{"metrics": {total_return_pct, cagr_pct, max_drawdown_pct, sharpe, win_rate_pct, trades, exposure_pct}, "equity": [{ts, equity}], "trades": [{symbol, entry_ts, entry_price, exit_ts, exit_price, qty, pnl, fees}], "warnings": [...]}`;
  - meno barre del necessario → `warnings=["insufficient_data:<symbol>"]` e 0 trade (Review Focus 5).
- `runner.py`: `class BacktestRunner`, ogni 2s:
  - prende i backtest `queued` (`FOR UPDATE SKIP LOCKED`) → `running`;
  - carica le barre con `router.get_bars`, prende la regola di commissione (`resolve_rule` per il portafoglio o la globale) e lo slippage;
  - esegue `run_backtest` in un thread (`asyncio.to_thread`) → `completed` con il risultato, oppure `failed` con l'errore;
  - al riavvio i backtest `running` da più di 10 minuti tornano `queued`.
- Metodi MCP:
  - `run_backtest(strategy, symbols, timeframe="1d", from_date, to_date, initial_cash="10000", params={}, portfolio_id?)` → `{backtest_id, status:"queued"}`; con `portfolio_id` serve `read`; più di 2 backtest in esecuzione per agente → `RATE_LIMITED`;
  - `get_backtest_result(backtest_id)` → stato e risultato; un backtest di un altro agente → FORBIDDEN.
- Test del motore, su serie sintetiche:
  - buy & hold su un rialzo lineare da 100 a 110, con commissione 0 e slippage 0 → +10% meno la frazione di cash che resta per l'arrotondamento al lotto;
  - `sma_cross` su una serie costruita con un incrocio noto → 1 trade alle date attese;
  - commissioni applicate;
  - `insufficient_data`;
  - max drawdown su una serie nota.
- Test del runner: queued → completed con `FakeMarketData`; un errore → failed.
- [ ] FAIL → implementa → PASS → commit `feat(backtest): engine, strategies, runner, MCP tools`

### Task 12: Verifica finale e documentazione

**Files:** `docs/paperclip-mcp.md` (elenco metodi aggiornato con watchlist, alert e backtest), `README.md` alla radice (in italiano: cos'è SMTrading, avvio con `docker compose up -d --build`, URL, primo login, token dell'agente, collegamento di Paperclip, dove inserire le chiavi API dei connettori e dei broker, backup), test e2e MCP aggiornato con l'elenco completo dei metodi della spec §9.

- Verifica:
  1. `uv run pytest -q` → tutto verde;
  2. `npm --prefix web test -- --run` → verde;
  3. `docker compose up -d --build`;
  4. lo smoke MCP ha exit 0 e il numero di metodi uguale a quello dell'elenco della spec;
  5. `bash scripts/e2e.sh` → verde.
- [ ] Commit `docs: README, paperclip MCP guide; final verification`
