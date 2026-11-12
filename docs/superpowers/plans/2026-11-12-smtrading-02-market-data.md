# SMTrading — Piano 2: Market data (connettori gratuiti, router, cache, streaming, indicatori, orari)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un modulo `marketdata` che fornisce quote, storici, news, fondamentali, calendario, cambi, ricerca e screener con fallback tra fornitori, cache su Redis e rate limit; streaming in tempo reale delle crypto nel worker, con polling di riserva; indicatori tecnici; orari dei mercati. Funziona senza nessuna chiave API.

**Architecture:** Ogni fornitore è una sottoclasse di `DataProvider` registrata in `registry.PROVIDERS`. Il `MarketDataRouter` sceglie il fornitore per (classe di asset, capacità) secondo le priorità salvate nel database, rispetta i rate limit (token bucket su Redis) e salva i risultati in cache. Lo `StreamManager` gira nel worker: mantiene gli stream per i simboli seguiti (set Redis `md:tracked`) e scrive ogni tick su Redis (`quote:{symbol}` + pub/sub `ticks:{symbol}`). Questo piano parte dal branch del piano 1.

**Tech Stack:** httpx (async), websockets, yfinance (in thread), exchange_calendars, cryptography (AES-GCM), redis-py asyncio, respx (mock HTTP nei test), pandas (solo nei test come implementazione di riferimento per gli indicatori; è già dipendenza di yfinance).

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§8, §9 metodi di mercato, §4 `connectors` e `provider_priorities`, §3 `worker`)

## Global Constraints

- Tutti i vincoli del Piano 1 valgono ancora: `Decimal` ovunque per prezzi e volumi, servizi senza commit, `SMTError`/`ErrorCode`, Python 3.12, uv.
- Simboli canonici come nel seed del piano 1: `AAPL`, `ENI.MI`, `SAP.DE`, `BTC-USD`, `EURUSD`. I fornitori traducono nel proprio formato; `Instrument.provider_symbols[<provider>]`, se presente, ha la precedenza.
- Timestamp sempre `datetime` con fuso UTC.
- Durata della cache: quote 5s; bars per timeframe `1m` 60s, `5m` 300s, `15m` 900s, `1h` 3600s, `1d`/`1wk` 21600s; news 300s; fondamentali 86400s; calendario 21600s; fx 60s; search 86400s; screener 900s.
- Polling di riserva per lo streaming: ogni 5s; riconnessione con attese 1, 2, 4, 8… fino a 60s.
- Connettori attivi al primo avvio: `yahoo`, `kraken`, `binance`, `coingecko`, `frankfurter`.
- Le chiavi e le configurazioni dei connettori sono salvate cifrate con AES-GCM; la chiave è `sha256(SMT_MASTER_KEY)`.
- Nessun test unit o di integrazione fa chiamate di rete reali (respx o server WebSocket locali); solo la verifica manuale del Task 11 usa la rete.

## Review Focus

1. **Tutti i fornitori falliscono o sono in rate limit** → `SMTError(PROVIDER_UNAVAILABLE)` con `details.errors` per fornitore, mai un'eccezione grezza. Test in Task 4.
2. **Simbolo sconosciuto o senza mapping per un fornitore** → quel fornitore viene saltato (trattato come non supportato) e si passa al successivo; se nessuno lo supporta → `SYMBOL_NOT_FOUND`. Test in Task 4.
3. **Lo stream si interrompe a metà** → si passa al polling entro 5s e si riconnette con le attese crescenti; le quote in `quote:{sym}` continuano ad aggiornarsi. Test in Task 9.
4. **Risposte dei fornitori con campi mancanti o null** (es. bid/ask assenti fuori orario su Yahoo) → `Quote` con `bid`/`ask` a `None` e `last` valorizzato, nessun crash. Test in Task 5, 6 e 7.
5. **Indicatori con meno barre del periodo** → valori `None` nelle prime posizioni, nessuna eccezione; serie vuota → lista vuota. Test in Task 10.

---

## File Structure

```
backend/src/smtrading/
  common/crypto.py                  # encrypt_json / decrypt_json (AES-GCM)
  marketdata/__init__.py
  marketdata/types.py               # Quote, Bar, Timeframe, NewsItem, Fundamentals, CalendarEvent, InstrumentHit, Capability, ScreenFilter
  marketdata/provider.py            # DataProvider (classe base), ProviderUnsupported
  marketdata/registry.py            # PROVIDERS: dict[str, type[DataProvider]]
  marketdata/serialization.py       # (de)serializzazione JSON delle dataclass con Decimal
  marketdata/cache.py               # cache_get/cache_set, write_quote/read_quote
  marketdata/ratelimit.py           # acquire() token bucket con script Lua
  marketdata/router.py              # MarketDataRouter
  marketdata/providers/frankfurter.py
  marketdata/providers/coingecko.py
  marketdata/providers/kraken.py    # REST + WS
  marketdata/providers/binance.py   # REST + WS
  marketdata/providers/yahoo.py     # yfinance
  marketdata/streaming.py           # StreamManager, track/untrack
  marketdata/indicators.py          # sma, ema, rsi, macd, bollinger, atr, vwap (pure)
  marketdata/indicators_service.py  # compute_indicators(router, symbol, timeframe, specs)
  marketdata/cli.py                 # python -m smtrading.marketdata.cli quote|bars ...
  connectors/__init__.py
  connectors/models.py              # Connector, ProviderPriority
  connectors/service.py             # list/set connector, get/set priorities, DEFAULT_PRIORITIES
  instruments/market_hours.py       # market_status(), MarketStatus
backend/migrations/versions/0002_connectors.py
backend/tests/fixtures/marketdata/*.json   # risposte reali registrate (vedi i task)
backend/tests/unit/test_crypto.py, test_serialization.py, test_indicators.py, test_market_hours.py
backend/tests/unit/providers/test_frankfurter.py, test_coingecko.py, test_kraken.py, test_binance.py, test_yahoo.py
backend/tests/integration/test_connectors_service.py, test_ratelimit_cache.py, test_router.py, test_streaming.py
```

---

### Task 1: Tipi, classe base dei fornitori, registro, cifratura

**Files:**
- Create: `common/crypto.py`, `marketdata/{__init__,types,provider,registry,serialization}.py`
- Test: `tests/unit/test_crypto.py`, `tests/unit/test_serialization.py`

**Interfaces:**
- Produces:
  - `encrypt_json(data: dict, master_key: str) -> bytes` (nonce di 12 byte + ciphertext) / `decrypt_json(blob: bytes, master_key: str) -> dict`. Con una chiave sbagliata → `SMTError(FORBIDDEN, "invalid master key")`.
  - `class Capability(StrEnum)`: `quote, bars, stream, news, fundamentals, calendar, fx, search, screener`.
  - `class Timeframe(StrEnum)`: `m1="1m", m5="5m", m15="15m", h1="1h", d1="1d", w1="1wk"`.
  - Dataclass frozen:
    - `Quote(symbol: str, bid: Decimal|None, ask: Decimal|None, last: Decimal|None, ts: datetime, provider: str)`
    - `Bar(ts: datetime, open: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal)`
    - `NewsItem(id: str, ts: datetime, headline: str, summary: str|None, url: str|None, source: str, symbols: tuple[str, ...])`
    - `Fundamentals(symbol: str, name: str|None, sector: str|None, industry: str|None, market_cap: Decimal|None, pe: Decimal|None, eps: Decimal|None, dividend_yield: Decimal|None, high_52w: Decimal|None, low_52w: Decimal|None, currency: str|None, extra: dict)`
    - `CalendarEvent(symbol: str, kind: Literal["earnings","dividend","split"], date: date, data: dict)`
    - `InstrumentHit(symbol: str, name: str, asset_class: AssetClass|None, exchange: str|None, currency: str|None, provider: str)`
    - `ScreenFilter(asset_class: AssetClass|None = None, sector: str|None = None, min_market_cap: Decimal|None = None, max_market_cap: Decimal|None = None, min_change_pct: Decimal|None = None, max_change_pct: Decimal|None = None, min_volume: Decimal|None = None, max_pe: Decimal|None = None, limit: int = 25)`
  - `class ProviderUnsupported(Exception)`: il fornitore non supporta la capacità o il simbolo.
  - `class DataProvider`:
    - `name: ClassVar[str]`, `capabilities: ClassVar[frozenset[Capability]]`, `asset_classes: ClassVar[frozenset[AssetClass]]`, `requires_key: ClassVar[bool]`, `default_rate_limit_per_min: ClassVar[int]`
    - `__init__(self, config: dict, http: httpx.AsyncClient)`
    - `def map_symbol(self, symbol: str, asset_class: AssetClass, override: str | None) -> str`: restituisce `override` se presente, altrimenti la mappatura di default del fornitore; se non è mappabile → `ProviderUnsupported`.
    - metodi async, che di default sollevano `ProviderUnsupported`:
      - `get_quotes(items: list[tuple[str, str]]) -> dict[str, Quote]` (coppie simbolo canonico, simbolo del fornitore)
      - `get_bars(symbol, provider_symbol, timeframe, start, end) -> list[Bar]`
      - `get_news(symbols: list[tuple[str,str]], limit: int) -> list[NewsItem]`
      - `get_fundamentals(symbol, provider_symbol) -> Fundamentals`
      - `get_calendar(symbols: list[tuple[str,str]], start: date, end: date) -> list[CalendarEvent]`
      - `get_fx_rate(base: str, quote: str) -> Decimal`
      - `search(query: str, limit: int) -> list[InstrumentHit]`
      - `screen(f: ScreenFilter) -> list[dict]`
      - `stream(items: list[tuple[str,str]]) -> AsyncIterator[Quote]`
      - `health_check() -> None`
  - `PROVIDERS: dict[str, type[DataProvider]]`, che parte vuoto; ogni task dei fornitori aggiunge il suo.
  - `serialization.dumps(obj) -> str` / `loads_quote(s) -> Quote`, `loads_bars(s) -> list[Bar]`, più un `loads(s, cls)` generico per le dataclass del modulo. I Decimal sono serializzati come stringhe, i datetime in ISO 8601.

- [ ] **Step 1: Test che falliscono**
```python
def test_encrypt_roundtrip():  assert decrypt_json(encrypt_json({"k": "v"}, "m"), "m") == {"k": "v"}
def test_wrong_key():          # decrypt_json(blob, "other") → SMTError FORBIDDEN
def test_nonce_random():       # due cifrature dello stesso dict sono diverse
def test_quote_roundtrip():
    q = Quote("BTC-USD", Decimal("1.10"), None, Decimal("1.2"), datetime(2026,1,1,tzinfo=UTC), "kraken")
    assert loads_quote(dumps(q)) == q
def test_bars_roundtrip(): ...
```
- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa** (aggiungi `cryptography`, `httpx`, `websockets` con `uv add`). **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `feat(marketdata): core types, provider base, crypto helpers`

---

### Task 2: Tabelle dei connettori, priorità di default, servizio

**Files:**
- Create: `connectors/{__init__,models,service}.py`, `migrations/versions/0002_connectors.py`
- Modify: `migrations/env.py` (import dei modelli), `seed.py` (chiama `seed_connectors`)
- Test: `tests/integration/test_connectors_service.py`

**Interfaces:**
- Consumes: `encrypt_json`/`decrypt_json`, `get_settings().master_key`, `AssetClass`, `Capability`.
- Produces:
  - `Connector`: `name PK str, enabled bool, config_encrypted bytea|None, rate_limit_per_min int|None, health_status str='unknown' ('ok'|'error'|'unknown'), last_error text|None, last_checked_at timestamptz|None`.
  - `ProviderPriority`: PK `(asset_class, capability)`, `providers JSONB list[str]`.
  - `DEFAULT_PRIORITIES: dict[tuple[AssetClass, Capability], list[str]]`:

    | capacità | us_equity | eu_equity | crypto | forex |
    |---|---|---|---|---|
    | quote | alpaca, finnhub, yahoo | twelvedata, finnhub, yahoo | kraken, binance, coingecko, yahoo | twelvedata, yahoo, frankfurter |
    | bars | alpaca, yahoo | twelvedata, yahoo | kraken, binance, yahoo | twelvedata, yahoo |
    | stream | alpaca, finnhub | twelvedata, finnhub | kraken, binance | twelvedata |
    | news | alpaca, finnhub, yahoo | finnhub, yahoo | yahoo | — |
    | fundamentals | finnhub, yahoo | finnhub, yahoo | coingecko | — |
    | calendar | finnhub, yahoo | finnhub, yahoo | — | — |
    | fx | — | — | — | frankfurter, twelvedata, yahoo |
    | search | yahoo | yahoo | coingecko, yahoo | yahoo |
    | screener | yahoo | yahoo | — | — |

  - `DEFAULT_ENABLED = {"yahoo","kraken","binance","coingecko","frankfurter"}`.
  - `async def seed_connectors(session) -> None`: inserisce le righe mancanti di connettori e priorità, senza mai sovrascrivere le modifiche dell'admin. I connettori seminati sono tutti i nomi presenti nella tabella delle priorità più `revolutx`.
  - `async def list_connectors(session) -> list[Connector]`
  - `async def get_connector_config(session, name: str) -> dict`: decifrata, `{}` se assente; connettore inesistente → `NOT_FOUND`.
  - `async def update_connector(session, name: str, *, enabled: bool | None = None, config: dict | None = None, rate_limit_per_min: int | None = None) -> Connector`
  - `async def record_health(session, name: str, ok: bool, error: str | None) -> None`
  - `async def get_priorities(session, asset_class: AssetClass, capability: Capability) -> list[str]`: legge dal database, con fallback su `DEFAULT_PRIORITIES`, altrimenti `[]`.
  - `async def set_priorities(session, asset_class, capability, providers: list[str]) -> None`: un nome sconosciuto (non presente tra i connettori) → `NOT_FOUND`.

- [ ] **Step 1: Test che falliscono**: seed idempotente (due esecuzioni → stesso conteggio; una modifica di `enabled` fatta dall'admin sopravvive a un nuovo seed), roundtrip della configurazione cifrata (in colonna c'è `bytes` diversi dal JSON in chiaro), `get_priorities(crypto, quote)` restituisce i default, `set_priorities` con un nome sconosciuto → `NOT_FOUND`.
- [ ] **Step 2: FAIL. Step 3: Implementa. Step 4: PASS.**
- [ ] **Step 5: Commit** `feat(connectors): connector config storage and provider priorities`

---

### Task 3: Rate limit e cache su Redis

**Files:**
- Create: `marketdata/ratelimit.py`, `marketdata/cache.py`
- Test: `tests/integration/test_ratelimit_cache.py` (con il container Redis)

**Interfaces:**
- Produces:
  - `async def acquire(redis, provider: str, per_minute: int, *, now: float | None = None) -> bool`: token bucket con capacità `per_minute` e ricarica `per_minute/60` token al secondo. Lo stato sta nella chiave Redis `rl:{provider}` (hash con `tokens` e `ts`) ed è aggiornato da uno script Lua atomico. Il parametro `now` serve ai test.
  - `async def cache_get(redis, key: str) -> str | None`; `async def cache_set(redis, key: str, value: str, ttl_s: int) -> None`
  - `async def write_quote(redis, q: Quote) -> None`: `HSET quote:{symbol}` con i campi `bid ask last ts provider` (stringhe; `""` per None), poi `PUBLISH ticks:{symbol} <json>`.
  - `async def read_quote(redis, symbol: str) -> Quote | None`
  - `CACHE_TTL: dict[str, int]` e `bars_ttl(tf: Timeframe) -> int` con i valori dei Global Constraints.

- [ ] **Step 1: Test che falliscono**: `per_minute=2` → con lo stesso `now` restituisce True, True, False; con `now+30` di nuovo True. `write_quote` → `read_quote` restituisce la stessa quota, e un subscriber su `ticks:BTC-USD` riceve il JSON. `cache_set` con ttl 1 → `cache_get` subito restituisce il valore.
- [ ] **Step 2: FAIL. Step 3: Implementa. Step 4: PASS.**
- [ ] **Step 5: Commit** `feat(marketdata): redis token-bucket rate limiter and cache helpers`

---

### Task 4: MarketDataRouter

**Files:**
- Create: `marketdata/router.py`
- Test: `tests/integration/test_router.py` (fornitori finti registrati nel test con `monkeypatch.setitem(PROVIDERS, ...)`)

**Interfaces:**
- Consumes: Task 1-3, `get_instrument` (piano 1), `get_priorities`, `get_connector_config`, `list_connectors`, `record_health`.
- Produces: `class MarketDataRouter`:
  - `__init__(self, sessionmaker: async_sessionmaker, redis, http: httpx.AsyncClient)`
  - `async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]`
  - `async def get_bars(self, symbol, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]`
  - `async def get_news(self, symbols: list[str], limit: int = 20) -> list[NewsItem]`
  - `async def get_fundamentals(self, symbol) -> Fundamentals`
  - `async def get_calendar(self, symbols: list[str], start: date, end: date) -> list[CalendarEvent]`
  - `async def get_fx_rate(self, base: str, quote: str) -> Decimal` (con `base == quote` → `Decimal(1)` senza chiamare nessuno)
  - `async def search(self, query: str, limit: int = 20) -> list[InstrumentHit]`, che unisce i risultati dei fornitori di search delle varie classi di asset, senza duplicati per simbolo
  - `async def screen(self, f: ScreenFilter) -> list[dict]`
  - `async def provider_for_stream(self, asset_class: AssetClass) -> DataProvider | None`: il primo fornitore attivo con capacità `stream`
  - `async def check_health(self) -> dict[str, bool]`: chiama `health_check` di ogni connettore attivo e registrato, poi `record_health`
- Algoritmo, uguale per ogni capacità:
  1. Si raggruppano i simboli per classe di asset tramite `get_instrument`. Per `fx` la classe è `forex`; per `search` si interrogano tutte le classi.
  2. Si legge la cache: per le quote `read_quote` con `now - ts <= max_age_s`, per il resto la chiave `md:{cap}:{args}`.
  3. Per ogni fornitore, nell'ordine di `get_priorities(...)`: si salta se non è in `PROVIDERS` o non è attivo; si salta se `acquire()` restituisce False (e si annota `rate_limited`); si chiama il metodo con un timeout di 10s. `ProviderUnsupported` → si passa al successivo senza annotare un errore; qualsiasi altra eccezione → si annota l'errore e si passa al successivo; in caso di successo si salva in cache e si restituisce.
  4. Per le quote con più simboli si procede per simbolo: quelli ancora mancanti passano al fornitore successivo.
  5. Fine dei fornitori: se tutti hanno risposto `ProviderUnsupported` → `SYMBOL_NOT_FOUND`; altrimenti → `PROVIDER_UNAVAILABLE` con `details={"errors": {provider: msg}}`.
  6. Le istanze dei fornitori sono create una volta sola, con la configurazione decifrata, e ricaricate quando la configurazione cambia (semplice: si ricaricano se sono più vecchie di 60s).

- [ ] **Step 1: Test che falliscono** (fornitori finti `fake_a`, `fake_b` per crypto/quote, priorità impostate nel test):
  - `test_first_provider_used_and_cached`: `fake_a` viene chiamato una volta; con una seconda chiamata entro 5s nessuna nuova chiamata.
  - `test_fallback_on_error`: `fake_a` solleva RuntimeError → il risultato viene da `fake_b`.
  - `test_disabled_provider_skipped`
  - `test_rate_limited_provider_skipped`: rate limit `fake_a` = 0 → si usa `fake_b`.
  - `test_all_fail_provider_unavailable`: codice + `details.errors` con 2 chiavi.
  - `test_all_unsupported_symbol_not_found`
  - `test_partial_quotes_merge`: `fake_a` restituisce solo BTC → ETH arriva da `fake_b`.
  - `test_fx_same_currency_returns_one`
  - `test_stream_quote_in_redis_used`: con `write_quote` fresco nessun fornitore viene chiamato.
- [ ] **Step 2: FAIL. Step 3: Implementa. Step 4: PASS.**
- [ ] **Step 5: Commit** `feat(marketdata): router with priorities, fallback, caching, rate limits`

---

### Task 5: Fornitori Frankfurter e CoinGecko

**Files:**
- Create: `marketdata/providers/{__init__,frankfurter,coingecko}.py`, `tests/fixtures/marketdata/frankfurter_latest.json`, `coingecko_simple_price.json`, `coingecko_search.json`, `coingecko_coin_bitcoin.json`
- Modify: `marketdata/registry.py`
- Test: `tests/unit/providers/test_frankfurter.py`, `test_coingecko.py`

**Interfaces:**
- `FrankfurterProvider`:
  - `name="frankfurter"`, capacità `{fx, quote}`, asset `{forex}`, nessuna chiave, 60 chiamate al minuto.
  - `get_fx_rate` usa `GET https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD`.
  - `get_quotes` per il forex `EURUSD` → `last` = cambio, `bid`/`ask` None.
  - `map_symbol("EURUSD") → "EUR/USD"` (base/quote interni); simboli non forex → `ProviderUnsupported`.
- `CoinGeckoProvider`:
  - `name="coingecko"`, capacità `{quote, search, fundamentals}`, asset `{crypto}`, nessuna chiave (header `x-cg-demo-api-key` se c'è `config["api_key"]`), 30 chiamate al minuto.
  - Mappa di default `BTC→bitcoin, ETH→ethereum, SOL→solana, XRP→ripple, ADA→cardano, DOGE→dogecoin, BNB→binancecoin, USDT→tether, USDC→usd-coin`; quote currency da `BTC-USD` → `usd`.
  - `get_quotes` usa `/api/v3/simple/price?ids=...&vs_currencies=usd&include_last_updated_at=true`.
  - `search` usa `/api/v3/search?query=`; i simboli canonici sono `<SYM>-USD`.
  - `get_fundamentals` usa `/api/v3/coins/{id}`: `market_cap`, massimi e minimi; `name`.
- **Fixture:** salva risposte **reali** facendo una sola chiamata con `curl` per ciascun endpoint, e committale. Nei test servile con `respx`.
- Test:
  - parsing corretto dei Decimal (nessun float: costruisci i Decimal da `str()` del valore JSON, oppure usa `json.loads(parse_float=Decimal)`);
  - simbolo non mappato → `ProviderUnsupported`;
  - HTTP 429 → eccezione `httpx.HTTPStatusError`, che il router tratta come errore;
  - campi mancanti → None.
- [ ] Step: test FAIL → implementa → PASS → commit `feat(marketdata): frankfurter and coingecko providers`

---

### Task 6: Fornitori Kraken e Binance (REST)

**Files:**
- Create: `marketdata/providers/{kraken,binance}.py` (solo la parte REST), fixture `kraken_ticker.json`, `kraken_ohlc.json`, `binance_book_ticker.json`, `binance_klines.json`
- Modify: `registry.py`
- Test: `tests/unit/providers/test_kraken.py`, `test_binance.py`

**Interfaces:**
- `KrakenProvider`:
  - `name="kraken"`, capacità `{quote, bars, stream}`, asset `{crypto}`, 60 chiamate al minuto.
  - `map_symbol("BTC-USD") → "XBTUSD"` (BTC→XBT, il resto invariato; `ETH-USD → ETHUSD`); i simboli non crypto → `ProviderUnsupported`.
  - `get_quotes` usa `GET https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD`: `bid=b[0]`, `ask=a[0]`, `last=c[0]`. La risposta usa chiavi come `XXBTZUSD`: abbinale confrontando il campo `altname` oppure provando in ordine i pair richiesti. Ruling ammesso: chiama `/0/public/AssetPairs?pair=` una volta e tieni in cache la mappa altname → chiave.
  - `get_bars` usa `/0/public/OHLC?pair=&interval=` con intervalli `1m→1, 5m→5, 15m→15, 1h→60, 1d→1440, 1wk→10080` e `since=start`, poi filtra fino a `end`.
  - Se il campo `error` non è vuoto → `RuntimeError(error)`.
- `BinanceProvider`:
  - `name="binance"`, capacità `{quote, bars, stream}`, asset `{crypto}`, 600 chiamate al minuto.
  - `map_symbol("BTC-USD") → "BTCUSDT"`: USD è approssimato con USDT. Documentalo nella docstring; è il motivo per cui Kraken ha la priorità.
  - `get_quotes` usa `GET https://api.binance.com/api/v3/ticker/bookTicker?symbols=[...]` per bid/ask e `/api/v3/ticker/price` per l'ultimo prezzo (oppure solo bookTicker con `last=(bid+ask)/2`; scegli bookTicker + price).
  - `get_bars` usa `/api/v3/klines?symbol=&interval=&startTime=&endTime=` con intervalli `1m,5m,15m,1h,1d,1w`.
- Test: parsing con le fixture reali; tutti i valori sono Decimal; mapping di BTC→XBT; `ProviderUnsupported` per `AAPL`; risposta Kraken con `error` → eccezione.
- [ ] Step: FAIL → implementa → PASS → commit `feat(marketdata): kraken and binance REST providers`

---

### Task 7: Fornitore Yahoo (yfinance)

**Files:**
- Create: `marketdata/providers/yahoo.py`, fixture `tests/fixtures/marketdata/yahoo_*.json` (dati restituiti da yfinance e serializzati: `fast_info`, `history`, `info`, `news`, `calendar`, `search`, `screen`)
- Modify: `registry.py`
- Test: `tests/unit/providers/test_yahoo.py`

**Interfaces:**
- `YahooProvider`:
  - `name="yahoo"`, capacità `{quote, bars, news, fundamentals, calendar, search, screener}`, asset: tutti, nessuna chiave, 60 chiamate al minuto.
  - **Isolamento:** tutte le chiamate a yfinance passano da un adapter sottile `_YF` (metodi `fast_info(sym)`, `history(sym, interval, start, end)`, `info(sym)`, `news(sym)`, `calendar(sym)`, `dividends(sym)`, `splits(sym)`, `search(q)`, `screen(query)`), eseguito con `asyncio.to_thread`. Nei test `_YF` viene sostituito da un finto che legge le fixture. La logica di conversione è testata, yfinance no.
  - `map_symbol`: us_equity ed eu_equity invariati; crypto `BTC-USD` invariato; forex `EURUSD → EURUSD=X`.
  - `get_quotes`: `last=fast_info.last_price`; `bid`/`ask` da `info` se disponibili, altrimenti None (Review Focus 4).
  - `get_bars`: intervalli `1m,5m,15m,1h→"60m",1d,1wk`.
  - `get_calendar`: earnings da `calendar`, dividend da `dividends`, split da `splits`, filtrati per `[start, end]`.
  - `screen(f)`: traduce `ScreenFilter` in una `yf.EquityQuery` (regione us ed eu in base a `asset_class`; sector, intradaymarketcap, percentchange, dayvolume, peratio.lasttwelvemonths), con `limit`. Restituisce dict `{symbol, name, price, change_pct, market_cap, volume, pe, sector}` con valori Decimal.
  - `health_check`: `fast_info("SPY")`.
- **Fixture:** genera i JSON una sola volta con uno script usa e getta che chiama yfinance in rete (non committare lo script), e commit dei JSON.
- Test: conversione di ogni metodo da fixture a dataclass; bid/ask mancanti → None; mapping del forex; intervallo 1h → "60m".
- [ ] Step: aggiungi `yfinance` con `uv add` → test FAIL → implementa → PASS → commit `feat(marketdata): yahoo provider via yfinance`

---

### Task 8: Streaming WebSocket di Kraken e Binance

**Files:**
- Modify: `marketdata/providers/kraken.py`, `binance.py`
- Test: `tests/unit/providers/test_kraken.py`, `test_binance.py` (aggiunte), più un server WebSocket locale nei test (`websockets.serve` su una porta casuale; l'URL WS del fornitore è configurabile con `config["ws_url"]`)

**Interfaces:**
- `KrakenProvider.stream(items) -> AsyncIterator[Quote]`:
  - si collega a `wss://ws.kraken.com/v2`, invia `{"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD",...]}}` (in WS v2 il simbolo è `BTC/USD`, non XBT);
  - per i messaggi `channel=="ticker"` e `type in ("snapshot","update")` emette `Quote(bid, ask, last)` per ogni elemento di `data`;
  - ignora heartbeat e status.
- `BinanceProvider.stream(items)`:
  - si collega a `wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/...`;
  - dai messaggi `data.b`/`data.a` → Quote con `last=None`.
- Le funzioni di parsing sono pure e testate a parte: `parse_kraken_ws(msg: dict, reverse_map: dict[str,str]) -> list[Quote]`, `parse_binance_ws(msg: dict, reverse_map) -> list[Quote]`.
- `stream()` termina con un'eccezione quando la connessione si chiude: la riconnessione è compito dello StreamManager.
- Test:
  - parser con messaggi reali, copiati dalla documentazione ufficiale o catturati con `websockets` una sola volta;
  - test end-to-end con il server locale che invia 2 messaggi e poi chiude → vengono emesse 2 Quote, poi viene sollevata l'eccezione.
- [ ] Step: FAIL → implementa → PASS → commit `feat(marketdata): kraken and binance websocket streams`

---

### Task 9: StreamManager e integrazione nel worker

**Files:**
- Create: `marketdata/streaming.py`
- Modify: `worker/main.py` (heartbeat + StreamManager in parallelo con `asyncio.TaskGroup`)
- Test: `tests/integration/test_streaming.py`

**Interfaces:**
- Consumes: `MarketDataRouter.provider_for_stream`, `get_quotes`, `write_quote`, `get_instrument`.
- Produces:
  - `async def track(redis, symbols: list[str], *, source: str) -> None` e `async def untrack(redis, symbols, *, source: str)`.
    - Mantengono i set `md:tracked:{source}` e l'unione `md:tracked`: un simbolo resta seguito finché almeno una fonte lo segue.
    - Le fonti previste sono `orders`, `positions`, `alerts`, `watchlists`, `admin`.
  - `async def tracked_symbols(redis) -> set[str]`
  - `class StreamManager`:
    - `__init__(self, router: MarketDataRouter, redis, sessionmaker, *, reconcile_every_s: float = 5, poll_every_s: float = 5, backoff_max_s: float = 60)`
    - `async def run(self) -> None`, un loop infinito. A ogni riconciliazione:
      - Si raggruppano i simboli seguiti per classe di asset. Per ogni classe con un fornitore di stream si tiene **un task di stream per (fornitore, classe di asset)** con l'insieme corrente dei simboli; se l'insieme cambia, il task viene riavviato.
      - Ogni Quote ricevuta viene scritta con `write_quote`.
      - Se lo stream solleva un'eccezione, i simboli passano al polling e la riconnessione riparte con le attese 1, 2, 4… fino a `backoff_max_s`.
      - I simboli senza fornitore di stream, o in fallback, sono interrogati in polling con `router.get_quotes(symbols, max_age_s=0)` ogni `poll_every_s`, e ogni quota viene scritta con `write_quote`.
    - `def status(self) -> dict`: `{"streams": {"kraken:crypto": {"symbols": [...], "state": "connected"|"backoff"}}, "polling": [...]}`. Viene pubblicato anche in Redis `md:stream_status` (JSON) a ogni riconciliazione, così la dashboard può leggerlo.
- Test, con un fornitore finto di stream registrato e intervalli brevi (0.05s):
  - `test_tracked_symbol_gets_streamed_quotes`: `track(["BTC-USD"])` → entro 1s `read_quote` non è None.
  - `test_untrack_stops_stream`
  - `test_stream_failure_falls_back_to_polling_and_reconnects`: lo stream finto fallisce la prima volta → la quota arriva dal polling → al secondo tentativo lo stream si riconnette (verificalo contando le connessioni).
  - `test_symbol_without_stream_provider_is_polled`
  - `test_multi_source_tracking`: `orders` e `admin` seguono BTC; con `untrack` da `admin` resta seguito.
- [ ] Step: FAIL → implementa → PASS → commit `feat(marketdata): stream manager with polling fallback, wired into worker`

---

### Task 10: Indicatori tecnici

**Files:**
- Create: `marketdata/indicators.py`, `marketdata/indicators_service.py`
- Test: `tests/unit/test_indicators.py`

**Interfaces:**
- Funzioni pure su `list[Decimal]` o `list[Bar]`, che restituiscono liste lunghe quanto l'input con `None` dove i dati non bastano:
  - `sma(values, period)`
  - `ema(values, period)`: `alpha = 2/(period+1)`, seme = SMA dei primi `period` valori
  - `rsi(values, period=14)`: smoothing di Wilder
  - `macd(values, fast=12, slow=26, signal=9) -> tuple[list, list, list]` (macd, signal, hist)
  - `bollinger(values, period=20, k=2) -> tuple[list, list, list]` (mid, upper, lower): deviazione standard della popolazione
  - `atr(bars, period=14)`: True Range + Wilder
  - `vwap(bars)`: cumulativo sull'intera serie, con prezzo tipico `(h+l+c)/3`
- Calcolo con `decimal.localcontext(prec=34)`; risultati arrotondati a 8 decimali.
- `@dataclass IndicatorSpec(name: Literal["sma","ema","rsi","macd","bollinger","atr","vwap"], params: dict)`
- `async def compute_indicators(router, symbol: str, timeframe: Timeframe, specs: list[IndicatorSpec], lookback: int = 200) -> dict`: carica `lookback + max(period) * 3` barre fino ad adesso e restituisce `{"symbol", "timeframe", "ts": [...], "<name>": [...] | {"macd": [...], "signal": [...], "hist": [...]}}`, solo le ultime `lookback` posizioni. Un nome di indicatore sconosciuto → `SMTError(INVALID_ORDER)` (il codice generico di input non valido).
- Test: confronto con un'implementazione di riferimento in pandas, scritta nel test (ewm con `adjust=False`, rolling, Wilder via `ewm(alpha=1/n)`), su una serie fissa di 60 prezzi; tolleranza 1e-6 dopo che si stabilizzano, cioè dall'indice 3×period in poi per i calcoli di Wilder ed EMA. Inoltre:
  - serie più corta del periodo → tutto None;
  - serie vuota → `[]`;
  - VWAP su 3 barre calcolato a mano.
- [ ] Step: FAIL → implementa → PASS → commit `feat(marketdata): technical indicators`

---

### Task 11: Orari dei mercati, CLI e verifica live

**Files:**
- Create: `instruments/market_hours.py`, `marketdata/cli.py`
- Test: `tests/unit/test_market_hours.py`

**Interfaces:**
- `@dataclass(frozen=True) class MarketStatus: market: str; is_open: bool; next_open: datetime | None; next_close: datetime | None`
- `def market_status(market: str, at: datetime) -> MarketStatus`:
  - `NASDAQ`→`XNAS`, `NYSE`→`XNYS`, `MTA`→`XMIL`, `XETRA`→`XETR` tramite `exchange_calendars`, sessioni regolari;
  - `CRYPTO`: sempre aperto, `next_open`/`next_close` = None;
  - `FX`: aperto da domenica 22:00 UTC a venerdì 22:00 UTC;
  - mercato sconosciuto → `SMTError(NOT_FOUND)`.
- `def all_market_status(at: datetime) -> list[MarketStatus]`
- `cli.py`: `python -m smtrading.marketdata.cli quote BTC-USD AAPL EURUSD`, `bars BTC-USD 1h --days 2`, `fx EUR USD`, `search apple`. Usa le impostazioni reali, il database e Redis (dentro il container `api`) e stampa JSON.
- Test (date fisse):
  - XNAS mercoledì 2026-11-11 15:00 UTC → aperto, `next_close` 21:00 UTC;
  - XNAS sabato → chiuso, `next_open` lunedì 14:30 UTC;
  - XNAS giorno del Ringraziamento 2026-11-26 → chiuso;
  - XMIL 2026-12-25 → chiuso;
  - FX sabato 12:00 UTC → chiuso, domenica 23:00 UTC → aperto;
  - CRYPTO → aperto;
  - "FOO" → NOT_FOUND.
- [ ] **Step 1-4:** test FAIL → implementa (`uv add exchange_calendars`) → PASS.
- [ ] **Step 5: Verifica live** `docker compose up -d --build`, poi `docker compose exec api python -m smtrading.marketdata.cli quote BTC-USD ETH-USD AAPL ENI.MI EURUSD` → una quota per ogni simbolo, con `provider` kraken/kraken/yahoo/yahoo/yahoo (frankfurter va bene per EURUSD se Yahoo fallisce). Poi `docker compose exec redis redis-cli SADD md:tracked BTC-USD` (aggiungi alla CLI il comando `track SYMBOL...`, che chiama `track(..., source="admin")`, e usa quello) e, entro 10s, `redis-cli HGETALL quote:BTC-USD` → `provider kraken`, timestamp recente.
- [ ] **Step 6: Commit** `feat: market hours, marketdata CLI, live verification`
