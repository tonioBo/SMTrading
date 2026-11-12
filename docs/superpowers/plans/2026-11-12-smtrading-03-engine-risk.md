# SMTrading — Piano 3: Motore ordini, rischio, margine, interessi ed eventi societari

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Il motore che accetta, valida, controlla sul rischio, mette in attesa ed esegue tutti i tipi di ordine sui portafogli simulati, registrando ogni esecuzione nel registro. Comprende short e leva con margine, liquidazione, perdita giornaliera, kill-switch, interessi, dividendi e split, snapshot dell'equity. Gira nell'`api` (inserimento ordini) e nel `worker` (trigger, coda, scadenze, job).

**Architecture:** Funzioni pure per prezzi e trigger (`engine/pricing.py`, `engine/triggers.py`). La valutazione del portafoglio dipende dal protocollo `QuoteSource` (implementato da `MarketDataRouter` del piano 2, finto nei test). `execute_fill` è l'unico punto che muove soldi per un ordine: una transazione con lock sulla riga del portafoglio. L'`OrderProcessor` del worker reagisce ai tick (`ticks:*`) e fa uno sweep ogni secondo. I job giornalieri sono idempotenti tramite `job_runs`. Gli eventi sono pubblicati su Redis `events` e i messaggi per gli agenti salvati in `notifications`. Questo piano parte dal branch del piano 2.

**Tech Stack:** come nei piani 1 e 2.

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§5, §6, §7, §12)

## Global Constraints

- Valgono tutti i vincoli dei piani 1 e 2.
- **Prezzo di esecuzione:**
  - market buy = `ask × (1 + slippage_bps/10000)`; market sell = `bid × (1 − slippage_bps/10000)`;
  - limit buy = `min(limit, prezzo market buy)`; limit sell = `max(limit, prezzo market sell)`: mai peggio del limite;
  - bid o ask mancante → `last ± last × default_spread_bps/2/10000`, con spread di default in bps: us_equity 2, eu_equity 5, crypto 5, forex 1;
  - il prezzo viene arrotondato al `tick_size`, per eccesso per i buy e per difetto per i sell.
- **Trigger:**
  - limit buy quando `ask ≤ limit`; limit sell quando `bid ≥ limit`;
  - stop buy quando `ask ≥ stop`; stop sell quando `bid ≤ stop`;
  - trailing sell: `stop = hwm × (1 − pct/100)` oppure `hwm − amount`; trailing buy: `stop = lwm × (1 + pct/100)` oppure `lwm + amount`.
- **Quota vecchia:** oltre `stale_quote_s[asset_class]` (default 60 per ogni classe) un ordine market viene rifiutato con `STALE_QUOTE`.
- **Margine** (le grandezze sono in EUR):
  - `exposure = Σ|qty × mark|`, con `mark` = mid se ci sono bid e ask, altrimenti `last`;
  - `initial_req = exposure / leverage`;
  - `maint_req = initial_req × maintenance_margin_pct/100`; `warn_req = initial_req × warning_margin_pct/100`;
  - `buying_power = equity × leverage − exposure`.
  - Liquidazione quando `equity < maint_req`, chiudendo prima le posizioni con il P&L non realizzato più negativo, finché `equity ≥ maint_req`.
- **Interessi:** `|saldo| × tasso_annuo/100/360` al giorno, alle 00:00 UTC. Tassi di default: margin_loan EUR 5, USD 6, altre valute 6; prestito titoli per gli short us_equity 3, eu_equity 3, crypto 10, forex 0.
- **Ordini DAY:** scadono a `next_close` del mercato al momento dell'inserimento; per CRYPTO e FX alla mezzanotte successiva nel fuso del portafoglio.
- **Perdita giornaliera:** misurata rispetto allo snapshot `day_open` (mezzanotte nel fuso del portafoglio). Superata la soglia: `reduce_only` fino alla mezzanotte successiva oppure fino allo sblocco dell'admin.
- **Profilo di rischio "Default" (seed):**
  - nessun limite per ordine o per posizione (null);
  - perdita giornaliera massima 10%;
  - whitelist vuota = tutto permesso;
  - short consentito.

- **Ordine dei lock:** prima il lock sulla riga del portafoglio (`FOR UPDATE`), poi il lock della catena (dentro `post_journal`). Nessun percorso, comprese liquidazione e reset, può prendere un lock su un portafoglio mentre tiene già il lock della catena.
- Quando dopo un `SMTError` di `post_journal` si vuole continuare nella stessa transazione (es. salvare un ordine `rejected`), la chiamata va racchiusa in `session.begin_nested()`.
- Ogni riga di cash, commissione e interesse passa da `quantize_money(…, ccy, rounding=…)`: l'arrotondamento per eccesso del cambio usa `ROUND_UP`. Solo le righe di posizione portano quantità non arrotondate, e `post_journal` rifiuta comunque più di 18 decimali o valori ≥ 1e20.
- Per scrivere il seed si usa il lock consultivo già presente in `run_seed`.

## Review Focus

1. **Lo stesso ordine eseguito due volte** (un tick e lo sweep in parallelo) → una sola esecuzione: il lock sul portafoglio e il controllo dello stato vengono fatti dentro la transazione. Test in Task 6.
2. **Vendita oltre la posizione long** in un portafoglio senza short → `SHORT_NOT_ALLOWED`; con lo short consentito la posizione si inverte correttamente (la parte long chiusa con P&L realizzato, il resto aperto short). Test in Task 4 e Task 6.
3. **Portafoglio EUR che compra un titolo USD senza avere USD** → journal FX con la commissione di cambio, poi il TRADE; con cash insufficiente anche dopo il cambio e leva 1 → `INSUFFICIENT_BUYING_POWER` e nessun journal parziale. Test in Task 6.
4. **Ordine inserito a mercato chiuso** → `QUEUED`, eseguito alla prima quota valida dopo l'apertura; un ordine DAY non eseguito scade alla chiusura. Test in Task 9.
5. **Riavvio del worker con stop già superato** → eseguito al prezzo corrente (peggiore dello stop), mai al prezzo dello stop. Test in Task 9.

---

## File Structure

```
backend/src/smtrading/
  engine/__init__.py
  engine/models.py         # Order, Fill, OrderType, OrderSide, OrderStatus, TimeInForce
  engine/pricing.py        # fill_price, round_to_tick, notional_to_qty (puro)
  engine/triggers.py       # evaluate(order_view, quote) -> TriggerResult; update_trailing (puro)
  engine/quotesource.py    # Protocol QuoteSource + FakeQuoteSource (per i test, nel pacchetto)
  engine/execution.py      # execute_fill
  engine/service.py        # place_order, preview_order, cancel/modify/get/list, close_position(s), bracket, oco
  engine/processor.py      # OrderProcessor (worker)
  positions/models.py      # Position (proiezione)
  positions/service.py     # apply_fill_to_position (puro + db), get_positions
  valuation/service.py     # value_portfolio -> PortfolioValuation
  risk/models.py           # RiskProfile, InterestRate, SystemState, AppSetting, EquitySnapshot, JobRun, Notification
  risk/settings.py         # get_stale_threshold, get_default_spread_bps, kill switch get/set
  risk/checks.py           # pre_trade_check
  risk/margin.py           # MarginMonitor: check_portfolio, liquidate
  risk/daily_loss.py       # check_daily_loss, reset_daily
  corporate/interest.py    # run_interest(day)
  corporate/actions.py     # run_corporate_actions(day)
  corporate/snapshots.py   # take_snapshots, ensure_day_open
  jobs/scheduler.py        # loop dei job nel worker
  events.py                # publish_event, notify
backend/migrations/versions/0004_engine.py
backend/tests/unit/test_pricing.py, test_triggers.py, test_positions_math.py
backend/tests/integration/test_valuation.py, test_execution.py, test_execution_short.py,
  test_risk_checks.py, test_order_service.py, test_processor.py, test_margin.py, test_daily_loss.py, test_jobs.py, test_engine_e2e.py
```

---

### Task 1: Schema del motore, modelli, seed del rischio

**Files:** Create `engine/models.py`, `positions/models.py`, `risk/models.py`, `migrations/versions/0004_engine.py`. Modify `portfolios/models.py`, `migrations/env.py`, `seed.py`. Test: `tests/integration/test_migrations.py` (aggiunte), `test_seed.py` (aggiunte).

**Interfaces — Produces:**
- `OrderType(StrEnum)`: `market, limit, stop, stop_limit, trailing_stop`
- `OrderSide(StrEnum)`: `buy, sell`
- `TimeInForce(StrEnum)`: `day, gtc`
- `OrderStatus(StrEnum)`: `new, held, queued, pending, triggered, filled, canceled, rejected, expired`. `held` = figlio di un bracket in attesa del padre; `queued` = mercato chiuso; `pending` = in attesa del trigger; `triggered` = stop scattato, diventato market/limit.
- `Order`:
  - `id`, `portfolio_id FK`, `agent_id str|None`, `client_order_id str|None` (UNIQUE(portfolio_id, client_order_id))
  - `symbol FK instruments`, `side`, `type`, `tif`
  - `qty NUMERIC|None`, `notional NUMERIC|None` (esattamente uno dei due), `limit_price`, `stop_price`, `trail_pct`, `trail_amount`, `trail_ref_price` (hwm o lwm), `filled_qty NUMERIC=0`, `avg_fill_price|None`
  - `status`, `parent_id FK orders|None`, `oco_group_id UUID|None`, `role str|None` ('entry'|'take_profit'|'stop_loss')
  - `expires_at|None`, `reject_code|None`, `reject_message|None`, `source str` ('agent'|'admin'|'system'), `broker_order_id|None`
  - `created_at`, `updated_at`
  - Indice su `(symbol, status)`.
- `Fill`: `id, order_id FK, portfolio_id, symbol, side, qty, price, quote_bid, quote_ask, quote_last, quote_ts, source ('sim'|'broker'), fee_amount, fee_currency, fee_rule_id|None, journal_id FK journals, created_at`.
- `Position`: PK `(portfolio_id, symbol)`, `qty` (con segno), `avg_price`, `realized_pnl` (nella valuta dello strumento), `opened_at|None`, `updated_at`.
- `RiskProfile`: `id, name UNIQUE, max_order_notional_eur|None, max_position_pct|None, max_daily_loss_pct|None, max_daily_loss_eur|None, allowed_symbols JSONB list, allowed_asset_classes JSONB list, allowed_markets JSONB list, short_allowed bool`.
- `InterestRate`: PK `(kind ('loan'|'borrow'), key str)` (valuta oppure classe di asset), `annual_pct`.
- `SystemState`: PK `key`, `value JSONB`, `updated_at`. Chiavi: `kill_switch` = `{"on": false}`.
- `AppSetting`: PK `key`, `value JSONB`. Chiavi: `stale_quote_s` = `{"us_equity":60,"eu_equity":60,"crypto":60,"forex":60}`; `default_spread_bps` = `{"us_equity":2,"eu_equity":5,"crypto":5,"forex":1}`.
- `EquitySnapshot`: `id, portfolio_id, ts, kind ('periodic'|'day_open'), local_date date|None, equity_eur, cash_eur, exposure_eur, margin_used_eur`. UNIQUE(portfolio_id, kind, local_date) per `day_open`.
- `JobRun`: PK `(job, key)`, `ran_at`.
- `Notification`: `id, portfolio_id|None, agent_id|None, kind str, payload JSONB, created_at`. Indice su `created_at`.
- Su `Portfolio` si aggiungono: `risk_profile_id FK|None`, `reduce_only_reason str|None`, `reduce_only_until timestamptz|None`.
- Seed (idempotente):
  - profilo "Default" (valori nei Global Constraints) e assegnazione al Demo se `risk_profile_id` è null;
  - tassi di interesse dei Global Constraints: loan EUR 5, USD 6, `*` 6; borrow us_equity 3, eu_equity 3, crypto 10, forex 0;
  - `AppSetting` e `SystemState` di default.
- I test aggiungono: tabelle presenti, il `test_migration_matches_models` esistente ancora verde, seed eseguito due volte senza duplicati, Demo con il profilo Default.
- [ ] Step: test FAIL → implementa → PASS → commit `feat(engine): schema for orders, fills, positions, risk, jobs`

---

### Task 2: Prezzi e trigger (puri)

**Files:** Create `engine/pricing.py`, `engine/triggers.py`. Test: `tests/unit/test_pricing.py`, `tests/unit/test_triggers.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class QuoteView: bid: Decimal|None; ask: Decimal|None; last: Decimal|None; ts: datetime`
- `def effective_bid_ask(q: QuoteView, default_spread_bps: int) -> tuple[Decimal, Decimal]`: se mancano sia bid/ask sia `last` → `SMTError(PROVIDER_UNAVAILABLE)`.
- `def market_price(side: OrderSide, q: QuoteView, *, slippage_bps: int, default_spread_bps: int) -> Decimal` (non arrotondato)
- `def round_to_tick(price: Decimal, tick: Decimal, side: OrderSide) -> Decimal`
- `def fill_price(order_type: OrderType, side, q, *, limit_price: Decimal|None, slippage_bps, default_spread_bps, tick) -> Decimal`: per limit e stop_limit applica il clamp al limite **dopo** l'arrotondamento.
- `def notional_to_qty(notional: Decimal, price: Decimal, lot: Decimal) -> Decimal`: arrotonda per difetto al lotto; un risultato pari a 0 → `SMTError(INVALID_ORDER)`.
- `@dataclass(frozen=True) class OrderView: type: OrderType; side: OrderSide; status: OrderStatus; limit_price; stop_price; trail_pct; trail_amount; trail_ref_price`
- `class TriggerAction(StrEnum)`: `none, fill, trigger_to_market, trigger_to_limit, update_trail`
- `@dataclass(frozen=True) class TriggerResult: action: TriggerAction; new_trail_ref: Decimal|None = None`
- `def evaluate(o: OrderView, bid: Decimal, ask: Decimal) -> TriggerResult`:
  - limit pending → `fill` se scatta;
  - stop pending → `trigger_to_market`; stop_limit → `trigger_to_limit`;
  - stop_limit triggered → si valuta come limit;
  - trailing pending: si aggiorna il riferimento (sell: `max(ref, bid)`; buy: `min(ref, ask)`), si ricalcola lo stop e, se scatta, `trigger_to_market` (che include anche `new_trail_ref`); altrimenti `update_trail` se il riferimento è cambiato, altrimenti `none`;
  - market (queued o new) → `fill`.
- Test con valori esatti:
  - buy market con ask 100, slippage 10bps, tick 0.01 → 100.10; sell con bid 99.995, tick 0.01 → 99.89 (99.995 × 0.999 = 99.895005, arrotondato per difetto);
  - limit buy 100 con ask 99 → 99.00; limit sell 100 con bid 101 → 101.00;
  - limit buy 100 con ask 99.95 e slippage 10bps → market 100.04995 → 100.05 → clamp 100.00;
  - quota senza bid e ask, last 50, crypto 5bps → bid 49.99875, ask 50.00125;
  - nessun prezzo → PROVIDER_UNAVAILABLE;
  - `notional_to_qty(1000, 190, 0.000001)` → 5.263157; lot 1 con 1000/1900 → INVALID_ORDER;
  - tutti i trigger (limit e stop su entrambi i lati, al limite con uguaglianza → scatta);
  - trailing sell 5%: ref 100, bid 103 → update_trail 103; poi bid 97.84 → trigger (stop 97.85); trailing buy con amount 2: ref 50, ask 49 → update 49; ask 51.0 → trigger (stop 51).
- [ ] Step: FAIL → implementa → PASS → commit `feat(engine): pure pricing and trigger evaluation`

---

### Task 3: QuoteSource e valutazione del portafoglio

**Files:** Create `engine/quotesource.py`, `valuation/service.py`, `risk/settings.py`. Test: `tests/integration/test_valuation.py`.

**Interfaces:**
- `class QuoteSource(Protocol)`: `async get_quotes(symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]`; `async get_fx_rate(base: str, quote: str) -> Decimal`. `MarketDataRouter` lo soddisfa già; se le firme differiscono, adatta qui con un wrapper sottile.
- `class FakeQuoteSource`: `set_quote(symbol, bid, ask, last, ts=None)`, `set_fx(base, quote, rate)`; il cambio inverso è calcolato automaticamente; con la stessa valuta → 1; un dato mancante → `SMTError(PROVIDER_UNAVAILABLE)`.
- `risk/settings.py`:
  - `async get_stale_threshold(session, asset_class) -> int`
  - `async get_default_spread_bps(session, asset_class) -> int`
  - `async is_kill_switch_on(session) -> bool`
  - `async set_kill_switch(session, on: bool) -> None`
- `@dataclass class PositionValuation: symbol; qty; avg_price; mark: Decimal; currency; market_value: Decimal (con segno, valuta dello strumento); market_value_eur; unrealized_pnl; unrealized_pnl_eur`
- `@dataclass class PortfolioValuation: portfolio_id; base_currency; cash: dict[str, Decimal]; collateral: dict[str, Decimal]; margin_loan: dict[str, Decimal]; positions: list[PositionValuation]; equity_eur; cash_eur; exposure_eur; initial_req_eur; maint_req_eur; warn_req_eur; buying_power_eur; ts`
- `async def value_portfolio(session, qs: QuoteSource, portfolio_id: UUID) -> PortfolioValuation`:
  - le quantità vengono dai saldi degli account `position` (fonte di verità), `avg_price` dalla proiezione `positions`;
  - `equity_eur = Σ cash + Σ collateral + Σ margin_loan + Σ market_value`, tutto convertito in EUR;
  - i requisiti di margine seguono i Global Constraints.
- Test (portafoglio creato con `create_portfolio` più journal registrati direttamente per simulare le posizioni):
  - solo cash: 100000 EUR → equity 100000, buying_power 100000 (leva 1);
  - 10 AAPL con mid 200, EURUSD 1.25 (cioè USD→EUR 0.8) e cash di 98400 EUR → equity 98400 + 1600 = 100000, exposure 1600;
  - short di −5 AAPL, collateral 1000 USD, mid 200 → market_value −1000 USD, contributo all'equity 0; exposure 800 EUR;
  - leva 2: buying_power = equity × 2 − exposure;
  - manca la quota di un simbolo in posizione → PROVIDER_UNAVAILABLE.
- [ ] Step: FAIL → implementa → PASS → commit `feat(valuation): portfolio valuation and quote source protocol`

---

### Task 4: Proiezione delle posizioni

**Files:** Create `positions/service.py`. Test: `tests/unit/test_positions_math.py`, `tests/integration/test_valuation.py` (aggiunte per `get_positions`).

**Interfaces:**
- `@dataclass(frozen=True) class PositionState: qty: Decimal; avg_price: Decimal; realized_pnl: Decimal`
- `def apply_fill(state: PositionState, side: OrderSide, qty: Decimal, price: Decimal) -> PositionState`, con `localcontext(prec=60)`:
  - fill nello stesso verso o da zero → nuovo `avg` = media ponderata;
  - fill in riduzione → `realized += (price − avg) × chiusa × segno(qty prima)`, con `avg` invariato;
  - inversione → chiusura completa con realizzo, poi apertura del resto a `price`;
  - quantità che torna a 0 → `avg = 0`.
- `async def upsert_position(session, portfolio_id, symbol, side, qty, price) -> Position`
- `async def get_positions(session, portfolio_id) -> list[Position]`: solo `qty ≠ 0`.
- Test:
  - buy 10 @100, buy 10 @110 → avg 105;
  - sell 5 @120 → realized 75, qty 15, avg 105;
  - sell 20 @100 → realized += 15 × (100 − 105) = −75 → totale 0, qty −5, avg 100;
  - buy 5 @90 su short −5 avg 100 → realized += 50, qty 0, avg 0.
- [ ] Step: FAIL → implementa → PASS → commit `feat(positions): position projection math`

---

### Task 5: `execute_fill`, percorso long con cash e cambio valuta

**Files:** Create `engine/execution.py`, `events.py`. Test: `tests/integration/test_execution.py`.

**Interfaces:**
- Consumes: `post_journal`, `get_or_create_account`, `get_balance` (piano 1), `quote_fee`/`compute_fx_fee`/`resolve_rule`/`load_rules` (piano 1), `apply_fill`/`upsert_position` (Task 4), pricing (Task 2), `value_portfolio` (Task 3).
- `events.py`:
  - `async publish_event(redis, type: str, payload: dict) -> None`: pubblica `{"type", "ts", **payload}` sul canale `events`. Il JSON serializza i Decimal come stringhe.
  - `async notify(session, *, portfolio_id, kind, payload, agent_id=None) -> Notification`
- `@dataclass class FillResult: order: Order; fill: Fill; journals: list[Journal]`
- `async def execute_fill(session, qs: QuoteSource, order_id: UUID, *, quote: Quote, redis=None, journal_type: JournalType = JournalType.TRADE) -> FillResult | None`:
  1. `SELECT … FROM portfolios WHERE id=… FOR UPDATE`; ricarica l'ordine con `FOR UPDATE`; se lo stato non è in `{new, queued, pending, triggered}` → `None` (idempotenza).
  2. Prezzo con `fill_price`, usando lo spread e lo slippage del portafoglio; quantità = `qty` oppure `notional_to_qty`.
  3. Controvalore nella valuta dello strumento `ccy`; commissione con `quote_fee` sul controvalore convertito nella valuta della regola.
  4. **Buy** (questo task copre solo il caso in cui la posizione esistente è ≥ 0): fabbisogno = controvalore in `ccy`, più la commissione se la sua valuta è `ccy`.
     - Se `cash[ccy]` non basta, si converte dalla valuta base con un journal `FX`: righe `cash[base] −a, system_fx[base] +a, system_fx[ccy] −b, cash[ccy] +b`. `b` è la mancanza, `a = b / rate(base→ccy)` arrotondato con `quantize_money` per eccesso; poi la commissione di cambio `compute_fx_fee` nella valuta base, nello stesso journal: `cash[base] −fee, system_fees[base] +fee`.
     - Se manca ancora cash e `leverage > 1` → prestito: righe `margin_loan[ccy] −x, cash[ccy] +x` dentro il journal TRADE. Con leva 1 → `INSUFFICIENT_BUYING_POWER`.
  5. Journal TRADE:
     - `position[symbol] +qty`, `system_equity[symbol] −qty`;
     - `cash[ccy] −controvalore`, `system_equity[ccy] +controvalore`;
     - `cash[fee_ccy] −fee`, `system_fees[fee_ccy] +fee`.
  6. **Sell** che riduce una posizione long: TRADE con la posizione −qty, `cash[ccy] +controvalore`, la commissione e, se esiste `margin_loan[ccy] < 0`, il rimborso del debito con il ricavato: `cash −r, margin_loan +r`, dove `r = min(ricavato, |debito|)`.
  7. Proiezione `upsert_position`; riga `Fill`; ordine con `status=filled`, `filled_qty`, `avg_fill_price`; `updated_at`.
  8. Se l'ordine ha `oco_group_id`, gli altri ordini aperti del gruppo passano a `canceled`. Se ha figli `held` (bracket), passano a `pending` nello stesso `oco_group_id`.
  9. Dopo il flush, `publish_event(redis, "order.filled", {...})` se c'è `redis`, e `track`/`untrack` del simbolo con la fonte `positions`.
  10. Tutto senza commit. Qualsiasi `SMTError` lascia la transazione da annullare a chi chiama.
- Test (con `FakeQuoteSource` e il seed di instruments, regola globale di default e portafoglio da 100000 EUR):
  - `test_buy_eur_stock_cash`: buy di 10 ENI.MI, ask 15.000 → TRADE con cash EUR −150.00 e commissione 1.15 (1 + 0,1% × 150 = 1.15) → saldo 99848.85; posizione 10 con avg 15.000.
  - `test_buy_usd_stock_with_fx`: buy di 10 AAPL, ask 200, slippage 0, EURUSD 1.25 → journal FX con b = 2000 USD + … La commissione è in EUR (regola EUR), quindi il fabbisogno USD è 2000: a = 1600.00 EUR, fee FX = 0,25% di 1600 = 4.00 EUR. La commissione di trade è 1 + 0,1% × 1600 (controvalore convertito in EUR) = 2.60 EUR. Cash EUR = 100000 − 1600 − 4 − 2.60 = 98393.40; cash USD = 0.
  - `test_sell_reduces_long_and_realizes`
  - `test_insufficient_cash_leverage_1`: buy da 200000 EUR → INSUFFICIENT_BUYING_POWER e, dopo il rollback del savepoint, nessun journal.
  - `test_leverage_2_borrows`: leva 2, buy da 150000 EUR di ENI → margin_loan EUR −(150000 + commissione − 100000), cash 0.
  - `test_sell_repays_loan`
  - `test_already_filled_returns_none`
  - `test_oco_sibling_canceled`, `test_bracket_children_activated`
- [ ] Step: FAIL → implementa → PASS → commit `feat(engine): execute_fill for long positions with fx and leverage`

---

### Task 6: `execute_fill`, short, copertura, inversione, concorrenza

**Files:** Modify `engine/execution.py`. Test: `tests/integration/test_execution_short.py`.

**Interfaces / Regole:**
- **Sell oltre la posizione long** (posizione dopo il fill < 0):
  - serve `RiskProfile.short_allowed` e `Instrument.shortable`, altrimenti `SHORT_NOT_ALLOWED`;
  - la parte che chiude il long segue il caso 6 del Task 5;
  - la parte short si registra con `position −q`, `system_equity +q`, `collateral[ccy] +controvalore_short`, `system_equity[ccy] −controvalore_short`;
  - si chiama `post_journal(..., allow_negative=frozenset({AccountKind.position}))`.
- **Buy su una posizione short** (copertura): il pagamento viene prima da `collateral[ccy]` (fino a esaurimento), poi da `cash[ccy]`, poi dal cambio e dal prestito come nel Task 5. Una copertura oltre lo short apre un long per il resto.
- **Controllo del margine nell'esecuzione:** dopo un fill che aumenta l'esposizione, si ricalcola `value_portfolio`; con `buying_power_eur < 0` → `INSUFFICIENT_BUYING_POWER`, che fa fallire anche l'esecuzione. I fill che riducono l'esposizione non sono mai bloccati.
- **Concorrenza:** test su `fresh_db` (fixture del piano 1). Due task chiamano `execute_fill` sullo stesso ordine in parallelo, ognuno con la sua sessione e il suo commit → un solo `Fill` e un solo journal TRADE.
- Test:
  - `test_short_sell_creates_collateral`: sell di 5 AAPL senza posizione, bid 200, EURUSD 1.25 → posizione −5, collateral USD 1000.00, commissione in EUR (1 + 0,1% × 800 = 1.80);
  - `test_short_not_allowed_profile`, `test_short_not_allowed_instrument`;
  - `test_cover_uses_collateral_first`: copertura di 5 @180 → collateral 1000 − 900 = 100 USD, posizione 0, realized +100 USD;
  - `test_flip_long_to_short`: long 10 → sell 15 → realized sui 10, short 5 a collateral;
  - `test_concurrent_execute_single_fill`.
- [ ] Step: FAIL → implementa → PASS → commit `feat(engine): shorts, covers, flips, concurrency-safe fills`

---

### Task 7: Controlli di rischio pre-trade

**Files:** Create `risk/checks.py`. Test: `tests/integration/test_risk_checks.py`.

**Interfaces:**
- `@dataclass class CheckResult: ok: bool; code: ErrorCode|None; message: str|None; estimated_price: Decimal|None; estimated_notional_eur: Decimal|None; estimated_fee: FeeQuote|None; estimated_fx_fee_eur: Decimal|None; buying_power_after_eur: Decimal|None`
- `async def pre_trade_check(session, qs: QuoteSource, *, portfolio: Portfolio, instrument: Instrument, side: OrderSide, qty: Decimal|None, notional: Decimal|None, order_type: OrderType, limit_price: Decimal|None, stop_price: Decimal|None) -> CheckResult`, che esegue in ordine e si ferma al primo errore:
  1. kill-switch → `KILL_SWITCH_ACTIVE`;
  2. portafoglio `locked` → `PORTFOLIO_LOCKED`;
  3. whitelist (`allowed_symbols`, `allowed_asset_classes`, `allowed_markets`, dove la lista vuota vuol dire tutto) → `SYMBOL_NOT_ALLOWED`;
  4. `validate_quantity` / `validate_price` (piano 1); serve esattamente uno tra `qty` e `notional` → `INVALID_ORDER`;
  5. la quota serve solo per la stima: con quota vecchia e ordine market → `STALE_QUOTE`; per gli altri tipi la stima usa `limit` o `stop` se presenti;
  6. `reduce_only`: l'ordine deve ridurre |posizione| senza invertirla → altrimenti `REDUCE_ONLY`;
  7. short: con la posizione che diventerebbe negativa, servono `short_allowed` e `shortable` → `SHORT_NOT_ALLOWED`;
  8. `max_order_notional_eur` → `RISK_LIMIT_EXCEEDED`; `max_position_pct` (|posizione dopo| × mark in EUR / equity × 100) → `RISK_LIMIT_EXCEEDED`;
  9. buying power, solo se l'esposizione aumenta: `buying_power_eur − controvalore_eur − commissione − fx_fee ≥ 0` → altrimenti `INSUFFICIENT_BUYING_POWER`.
- Test: un caso per ogni codice di errore, un caso ok con le stime popolate (prezzo, commissione, fx_fee per uno strumento USD) e un ordine che riduce la posizione con buying power negativo che resta ok.
- [ ] Step: FAIL → implementa → PASS → commit `feat(risk): pre-trade checks`

---

### Task 8: Servizio ordini

**Files:** Create `engine/service.py`. Test: `tests/integration/test_order_service.py`.

**Interfaces:**
- `@dataclass class PlaceOrderRequest`: `portfolio_id; symbol; side; type; tif=TimeInForce.gtc; qty=None; notional=None; limit_price=None; stop_price=None; trail_pct=None; trail_amount=None; client_order_id=None; agent_id=None; source="agent"`
- `async def preview_order(session, qs, req) -> CheckResult`
- `async def place_order(session, qs, req, *, redis=None, now: datetime|None = None) -> Order`:
  - **Idempotenza:** stesso `(portfolio_id, client_order_id)` → restituisce l'ordine esistente se i parametri coincidono, altrimenti `INVALID_ORDER` con "client_order_id reused".
  - Validazione dei parametri per tipo: limit richiede `limit_price`; stop richiede `stop_price`; stop_limit richiede entrambi; trailing richiede esattamente uno tra `trail_pct` e `trail_amount`; `notional` è ammesso solo per market → altrimenti `INVALID_ORDER`.
  - `pre_trade_check` fallito → l'ordine viene **salvato** con `status=rejected`, `reject_code` e `reject_message`, e poi viene sollevato `SMTError` con quel codice. Chi chiama fa il commit anche in caso di rifiuto: l'API e l'MCP del piano 4 fanno commit e poi rilanciano.
  - Mercato chiuso (`market_status`) → `queued` (`MARKET_CLOSED_QUEUED` è informativo: l'ordine è accettato).
  - Mercato aperto + market → `execute_fill` subito con la quota corrente; limit, stop e trailing → `pending` (trailing con `trail_ref_price` = bid per i sell, ask per i buy) e una valutazione immediata con `evaluate`: se scatta subito, si esegue.
  - `tif=day` → `expires_at` secondo i Global Constraints.
  - `track(symbol, source="orders")`.
- `async def place_bracket_order(session, qs, req: PlaceOrderRequest, *, take_profit: Decimal, stop_loss: Decimal, redis=None) -> list[Order]`: ordine di ingresso (market o limit) più i figli `held` (take profit limit con `role=take_profit`, stop loss stop con `role=stop_loss`) sul lato opposto, con la stessa quantità e lo stesso `oco_group_id`. La validazione richiede, per un ingresso buy, `take_profit > riferimento > stop_loss` (al contrario per un sell); il riferimento è `limit_price` oppure l'ask o il bid.
- `async def place_oco_order(session, qs, a: PlaceOrderRequest, b: PlaceOrderRequest, *, redis=None) -> list[Order]`: stesso portafoglio e simbolo, tipi non market.
- `async def cancel_order(session, order_id, *, by: str) -> Order`: solo dagli stati aperti, altrimenti `INVALID_ORDER`. Annullando un ingresso `held` o `pending` vengono annullati anche i figli.
- `async def cancel_all_orders(session, portfolio_id, symbol: str|None = None) -> int`
- `async def modify_order(session, qs, order_id, *, qty=None, limit_price=None, stop_price=None, trail_pct=None, trail_amount=None) -> Order`: solo su `pending`, `queued` o `held`; rieseguono i controlli di rischio; un cambio di tipo non è ammesso.
- `async def get_order(session, order_id) -> Order` (`NOT_FOUND`); `async def list_orders(session, portfolio_id, *, status: list[OrderStatus]|None = None, limit=100) -> list[Order]`
- `async def close_position(session, qs, portfolio_id, symbol, qty: Decimal|None = None, *, source="agent", agent_id=None) -> Order`: ordine market sul lato opposto, per `|qty|`. Senza posizione → `INVALID_ORDER`.
- `async def close_all_positions(session, qs, portfolio_id, **kw) -> list[Order]`
- Test:
  - market eseguito subito;
  - limit `pending` e poi eseguito con un nuovo tick (chiamando direttamente `execute_fill` dopo `evaluate`);
  - rifiuto salvato con `reject_code`;
  - idempotenza (stessi parametri → stesso id; parametri diversi → errore);
  - mercato chiuso → `queued` (`now` = sabato);
  - bracket con figli `held` → padre eseguito → figli `pending` → uno eseguito → l'altro `canceled`;
  - `cancel` di un ingresso bracket annulla i figli;
  - `modify` di un limit;
  - `close_position` su long e su short;
  - `notional` con limit → `INVALID_ORDER`;
  - trailing senza parametri → `INVALID_ORDER`.
- [ ] Step: FAIL → implementa → PASS → commit `feat(engine): order service (place/preview/cancel/modify/bracket/oco/close)`

---

### Task 9: OrderProcessor nel worker

**Files:** Create `engine/processor.py`. Modify `worker/main.py`. Test: `tests/integration/test_processor.py`.

**Interfaces:**
- `class OrderProcessor`:
  - `__init__(self, sessionmaker, redis, qs: QuoteSource, *, sweep_every_s: float = 1.0, min_eval_interval_s: float = 0.25, clock=lambda: datetime.now(UTC))`
  - `async def run(self) -> None`: `PSUBSCRIBE ticks:*` più un loop di sweep in un TaskGroup.
  - `async def on_tick(self, symbol: str, quote: Quote) -> int`: numero di ordini eseguiti o scattati. Throttle per simbolo a `min_eval_interval_s`.
  - `async def sweep(self) -> None`:
    - scadenza degli ordini DAY con `expires_at ≤ now` → `expired`;
    - gli ordini `queued` dei mercati aperti vengono eseguiti con `qs.get_quotes`;
    - gli ordini `pending` e `triggered` dei mercati aperti vengono valutati con le quote correnti (il fallback per i simboli senza stream e la ripresa dopo un riavvio passano da qui).
  - Per ogni ordine c'è una transazione separata, con commit. Gli errori di un ordine vengono loggati e non fermano gli altri. Un `SMTError` durante l'esecuzione di un ordine scattato (es. buying power) porta l'ordine a `rejected` con il codice.
  - `trigger_to_market` porta l'ordine a `triggered` con tipo effettivo market (il tipo originale resta in `type`; lo scatto è registrato con lo stato `triggered`), poi lo esegue nello stesso giro. `trigger_to_limit` porta a `triggered`, e da lì in poi l'ordine è valutato come limit.
  - `update_trail` salva `trail_ref_price`.
- In `worker/main.py`: `OrderProcessor(router)` gira insieme a heartbeat e StreamManager.
- Test (`FakeQuoteSource` più chiamate dirette a `on_tick` e `sweep` con un `clock` controllato):
  - un limit buy scatta con il tick → eseguito e committato;
  - uno stop sell scatta → eseguito al bid × (1 − slippage), non al prezzo dello stop (Review Focus 5: stop 95, il tick arriva con bid 90 → prezzo di esecuzione ≈ 90);
  - trailing: due tick aggiornano il riferimento, il terzo lo fa scattare;
  - un ordine `queued` nel weekend viene eseguito dallo sweep quando il `clock` arriva a lunedì all'apertura;
  - un ordine DAY scade allo sweep dopo la chiusura;
  - due `on_tick` concorrenti sullo stesso simbolo → un solo fill;
  - un errore su un ordine non blocca l'ordine successivo.
- [ ] Step: FAIL → implementa → PASS → commit `feat(engine): worker order processor (ticks, sweep, queue, expiry)`

---

### Task 10: Margine, liquidazione, perdita giornaliera, kill-switch

**Files:** Create `risk/margin.py`, `risk/daily_loss.py`, `corporate/snapshots.py`. Modify `worker/main.py` e `engine/processor.py` (verifica del margine dopo ogni tick per i portafogli che hanno posizioni su quel simbolo e usano leva o short). Test: `tests/integration/test_margin.py`, `test_daily_loss.py`.

**Interfaces:**
- `async def check_portfolio_margin(session, qs, portfolio_id, *, redis=None) -> Literal["ok","warning","liquidated"]`:
  - con `equity < warn_req` → `notify(kind="margin_warning")` più l'evento `margin.warning`, al massimo una notifica ogni 15 minuti per portafoglio (controllata sull'ultima `Notification`);
  - con `equity < maint_req` → `liquidate`.
- `async def liquidate(session, qs, portfolio_id, *, redis=None) -> list[Order]`: ordina le posizioni per `unrealized_pnl_eur` crescente; per ciascuna inserisce un ordine market `source="system"` che chiude l'intera posizione e lo esegue subito con `execute_fill(..., journal_type=JournalType.LIQUIDATION)`; si ferma quando `equity ≥ maint_req`. Evento `liquidation` più `notify`.
- `async def margin_sweep(sessionmaker, qs, redis) -> None`: tutti i portafogli con margin_loan < 0 o posizioni short; il worker lo esegue ogni 60s.
- `async def ensure_day_open(session, qs, portfolio, now) -> EquitySnapshot`: crea lo snapshot `day_open` per la data locale, se manca.
- `async def take_snapshots(sessionmaker, qs) -> None`: snapshot `periodic` di tutti i portafogli, ogni 5 minuti.
- `async def check_daily_loss(session, qs, portfolio, now) -> bool`: con `max_daily_loss_pct` o `max_daily_loss_eur` superati → `status=reduce_only`, `reduce_only_reason="daily_loss"`, `reduce_only_until` = mezzanotte locale successiva, più `notify`. Restituisce True se ha appena bloccato il portafoglio. Viene chiamata dopo ogni fill (nel processor e nel service) e dentro lo sweep del margine.
- `async def reset_daily(session, now) -> int`: i portafogli con `reduce_only_reason="daily_loss"` e `reduce_only_until ≤ now` tornano `active`.
- `async def set_kill_switch_and_cancel(session, on: bool, *, cancel_open: bool) -> int`: numero di ordini annullati.
- Test:
  - leva 2, long ENI che crolla: il mid passa da 15 a 9 → warning poi liquidazione, posizione chiusa, journal LIQUIDATION;
  - liquidazione in ordine: prima la posizione più in perdita, e si ferma quando torna in regola (due posizioni, ne basta chiudere una);
  - throttle del warning;
  - perdita giornaliera 10% → `reduce_only` e un nuovo buy viene rifiutato con `REDUCE_ONLY`; `reset_daily` dopo mezzanotte → `active`;
  - kill-switch con `cancel_open=True` annulla gli ordini pending.
- [ ] Step: FAIL → implementa → PASS → commit `feat(risk): margin monitor, liquidation, daily loss, kill switch`

---

### Task 11: Interessi, dividendi e split, scheduler dei job

**Files:** Create `corporate/interest.py`, `corporate/actions.py`, `jobs/scheduler.py`. Modify `worker/main.py`. Test: `tests/integration/test_jobs.py`.

**Interfaces:**
- `async def run_job_once(session, job: str, key: str) -> bool`: `INSERT INTO job_runs … ON CONFLICT DO NOTHING RETURNING`. True = da eseguire.
- `async def run_interest(session, qs, day: date) -> int` (job `interest`, key `day.isoformat()`):
  - per ogni portafoglio e per ogni `margin_loan[ccy] < 0`: `i = quantize_money(|bal| × tasso_loan(ccy)/100/360)`;
  - per ogni posizione short: `i = |qty × mark| × tasso_borrow(asset_class)/100/360` nella valuta dello strumento;
  - journal INTEREST con `cash[ccy] −i`, `system_interest[ccy] +i`; se il cash in `ccy` non basta, l'interesse va su `margin_loan[ccy] −i` (sempre consentito);
  - restituisce il numero di journal creati.
- `async def run_corporate_actions(session, qs, day: date) -> int` (job `corporate`, key `day`):
  - per i simboli con posizione ≠ 0, `qs.get_calendar(symbols, day, day)`. Per questo aggiungi `get_calendar` al protocollo `QuoteSource` e a `FakeQuoteSource` (`set_calendar`).
  - dividendo (`data["amount"]`, `data["currency"]`, ex-date = `day`): journal DIVIDEND con `cash[ccy] ±qty × amount` (accredito sui long, addebito sugli short), contropartita `system_equity`;
  - split (`data["ratio"]`, es. 4 per 4:1): journal CORPORATE_ACTION con `position +qty × (r − 1)`, `system_equity −…`; nella proiezione `avg_price /= r`;
  - idempotenza aggiuntiva per (simbolo, giorno, tipo) con `run_job_once(session, "corp", f"{symbol}:{day}:{kind}")`.
- `class JobScheduler`: `run()` è un loop che ogni 30s:
  - alle 00:00 UTC (o appena dopo) esegue `run_interest(oggi − 1)` (ruling: gli interessi si calcolano sul giorno appena concluso);
  - alle 06:00 UTC esegue `run_corporate_actions(oggi)`;
  - ogni 5 minuti esegue `take_snapshots`;
  - ogni minuto, per ogni portafoglio, esegue `ensure_day_open` e `reset_daily`;
  - ogni 60s esegue `margin_sweep`;
  - ogni job ha la sua transazione e il suo commit; gli errori vengono loggati senza fermare il loop.
- Test:
  - interessi su un margin_loan EUR di −36000 al 5% → 5.00 EUR; seconda esecuzione nello stesso giorno → 0 journal;
  - interessi su uno short di 5 AAPL @200 USD al 3% → `quantize(1000 × 3/100/360)` = 0.08 USD, preso dal cash USD o dal margin_loan;
  - dividendo di 0.25 USD su 10 AAPL → cash USD +2.50; su uno short di −10 → −2.50;
  - split 4:1 su 10 a 200 → qty 40, avg 50;
  - corporate idempotente.
- [ ] Step: FAIL → implementa → PASS → commit `feat(corporate): interest, dividends, splits, job scheduler`

---

### Task 12: Scenario end-to-end e verifica in Docker

**Files:** Create `tests/integration/test_engine_e2e.py`, più il comando CLI `engine` in `marketdata/cli.py` (oppure un nuovo `smtrading/cli.py`: `order place --portfolio Demo --symbol BTC-USD --side buy --qty 0.01`, `positions --portfolio Demo`, `valuation --portfolio Demo`).

- Scenario (con `FakeQuoteSource` su `fresh_db`, che committa davvero):
  1. Demo con 100000 EUR;
  2. buy market di 0.5 BTC-USD (con FX);
  3. limit sell sopra il prezzo, poi un tick lo esegue;
  4. bracket su AAPL;
  5. short di ENI.MI e poi copertura;
  6. job degli interessi.
- Alla fine: `verify_chain().ok` è True, `verify_balances()` è `[]`, la somma dei P&L realizzati nella proiezione coincide con il calcolo indipendente fatto nel test, ogni journal è bilanciato.
- **Verifica in Docker:** `docker compose up -d --build`, poi `docker compose exec api python -m smtrading.cli order place --portfolio Demo --symbol BTC-USD --side buy --qty 0.01` → ordine `filled`; poi `positions` → BTC-USD 0.01; poi `valuation` → l'equity è circa 100000 meno le commissioni.
- [ ] Step: scrivi il test e verificalo → PASS → verifica in Docker → commit `test(engine): end-to-end scenario and CLI`
