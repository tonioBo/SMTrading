# SMTrading — Lavoro in giornata (intraday), costruito e spento

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** sorveglianza dei segnali su barre brevi con sveglia dell'agente, posizioni «di giornata», chiusura di sicurezza prima della fine seduta e regola USA sui conti piccoli (PDT), tutto dietro l'interruttore per portafoglio `intraday_enabled` (spento ovunque), provato su un portafoglio di prova nascosto con un nuovo conto di prova Alpaca.

**Architecture:**
- Nuovo pacchetto `intraday/`: calendario delle chiusure di barra (puro), posizioni di giornata e conteggio PDT (dai riempimenti, nessuno stato nuovo da allineare), sorveglianza (riusa `signals.service.strategy_signals` con un selettore), sveglia (scrittore di compiti Paperclip generalizzato al destinatario), rete di fine seduta (riusa `engine.service.close_position`), lettore PDT del broker e un unico job del worker `IntradayJob`.
- Con nessun portafoglio `intraday_enabled` il job fa **una** query e torna a dormire: niente Redis, niente prezzi, niente Paperclip. Il controllo di rischio PDT e l'avviso sulle vendite si attivano solo su portafogli/collegamenti con un portafoglio di giornata.
- Gli agenti non vedono nulla: i DTO MCP sono liste esplicite di campi (`mcp/dto.py ORDER_FIELDS`, `mcp/tools_portfolio.py`), i nuovi campi stanno solo nelle API admin e nel portale.
- Ramo `feat/intraday` (worktree) da `main`; migrazione `0046_intraday.py` (head attuale `0045` `portfolios.code`).

**Tech Stack:** come i piani precedenti (FastAPI, SQLAlchemy async, Alembic, Redis, httpx/respx, exchange_calendars, React + TanStack Query + Vitest). Nessuna dipendenza nuova.

**Spec:** `docs/superpowers/specs/2026-11-26-smtrading-intraday-design.md` (approvato il 26/11). Percorsi Python relativi a `backend/src/smtrading/`, test relativi a `backend/`; comandi da `backend/`: `uv run pytest -q -p no:cacheprovider <path>`.

## Global Constraints

- Valgono i vincoli dei piani 1-17: `Decimal` (mai float), `json_safe`/`to_jsonable`, `SMTError` con `ErrorCode`, audit admin con `write_audit`, nessun I/O di rete con una transazione aperta o sotto lock (R9), testi per l'utente in **italiano**, nessun segreto (chiavi broker, chiave board Paperclip) in log, errori, audit, outbox.
- **Interruttore:** tutto dietro `portfolios.intraday_enabled` (bool, default `false`, `server_default false`). Con l'interruttore spento ovunque il comportamento di oggi non cambia: test di non regressione nel Task 1 (catalogo MCP) e in ogni task che tocca un percorso esistente (ordini, rischio, segnali, protezione).
- **Strumenti MCP invariati:** nomi, descrizioni e schemi identici a oggi. Impronta del catalogo (`mcp.catalog.catalog_fingerprint`) sul commit `fd70037`: `fa77dcbd98f5c075a88b8cc8e1f902fb2677674ce2bbfc931a19d393593b71b0`. Nessun nuovo campo nelle risposte MCP (`ORDER_FIELDS`, `list_portfolios`, `_profile_dto` restano come sono). `day_trade` si imposta solo da portale/API interne.
- **Nessun nome fisso:** nessun nome o codice di agente/portafoglio nel codice nuovo. Il destinatario della sveglia è `portfolios.intraday_wake_agent_id` (agente con permesso `trade` sul portafoglio); il nome Paperclip si risolve da `agents.name`. `RISK_MANAGER` resta solo dove è oggi (protezione).
- **Valori dello spec:** timeframe ammessi `5m`, `15m`, `1h` (default `["15m","1h"]`); `day_close_minutes` default **10**; `intraday_max_wakes_per_hour` default **6**; margine del limite di sicurezza dal profilo di rischio, default **0,5%**; PDT: **3** operazioni in **5** sedute mobili (calendario `NYSE`), conti con margine e patrimonio sotto **25.000 $**.
- **Rete di sicurezza:** solo a mercato aperto (`market_status(...).is_open` e `now < chiusura della seduta`); forbice oltre `MAX_SPREAD` (`marketdata.quality.is_wide`) → limite all'ultimo scambio meno il margine; senza ultimo scambio con forbice larga nessun ordine e avviso; un avviso Telegram (`category="system"`) per **ogni** chiusura di sicurezza e uno se a 1 minuto dalla fine la quantità di giornata è ancora aperta.
- **PDT:** si rifiutano **solo** gli acquisti `day_trade` su portafogli `intraday_enabled` a budget esaurito (`ErrorCode.DAY_TRADE_LIMIT`); le vendite **mai**; i portafogli senza `intraday_enabled` ricevono solo un avviso Telegram.
- **Telegram:** sempre `source="system", category="system"` (mai il flusso attività che gli agenti leggono).
- **Messa in linea:** solo ad agenti Paperclip fermi, backup prima, migrazione applicata, `intraday_enabled=false` ovunque verificato da query. I documenti in `docs/paperclip-agents/` non si toccano.
- **Fuori da questo piano:** la ricerca delle fonti di prezzi in tempo reale a pagamento (gestita a parte); istruzioni agli agenti e accensione (dopo il 23/12).

## Review Focus

1. **Posizione mista (parte comprata ieri + parte di giornata) alla rete di fine seduta** → si vende solo la quantità di giornata; la parte di ieri resta; il messaggio dice di riproteggere la quantità restante (R16 annulla stop/obiettivo del simbolo). Test in Task 10.
2. **Limite di sicurezza non eseguito** (forbice larga, prezzo che scappa) → a 1 minuto dalla fine un avviso «chiusura di sicurezza non eseguita», nessun secondo ordine, nessun ordine a mercato chiuso. Test in Task 10.
3. **Riavvio del worker o Redis svuotato in seduta** → nessuna sveglia doppia: una chiave mancante si inizializza al valore attuale senza scatto; una barra già lavorata (`job_runs`) non si rilavora; un compito rimasto `pending` dopo un timeout si adotta con `open_issue_with_title`. Test in Task 9 e Task 11.
4. **Agente destinatario tolto dal portafoglio, declassato a `read` o disattivato dopo la configurazione** → nessun compito, esito `no_agent` e avviso Telegram con lo stesso testo; il PATCH rifiuta un agente senza `trade`. Test in Task 9 e Task 12.
5. **Entrate di giornata ancora aperte o a riposo quando il budget PDT è quasi finito** (budget 1, due entrate su titoli diversi nella stessa seduta) → la seconda è rifiutata: le posizioni di giornata aperte e gli acquisti `day_trade` a riposo contano come operazioni future, perché Alpaca rifiuterebbe poi la vendita che chiude la quarta. Test in Task 5.

---

## File Structure

```
backend/src/smtrading/
  intraday/__init__.py         vuoto
  intraday/models.py           IntradayTrigger (tabella intraday_triggers), costanti degli esiti
  intraday/calendar.py         bar_closes, due_bar_close, session_window, day_close_at, last_sessions, session_after
  intraday/day_positions.py    day_quantities
  intraday/pdt.py              FillRow, count_day_trades, pdt_applies, PdtBudget, link_budget, entry_budget, warn_day_trade_sell
  intraday/pdt_reader.py       refresh_pdt_states
  intraday/watch.py            Trigger, intraday_selector, evaluate_close
  intraday/wake.py             wake_title, wake_text, Waker
  intraday/day_close.py        close_limit_price, DayCloser
  intraday/summary.py          day_summary_text
  intraday/job.py              IntradayJob
  admin/routes_intraday.py     /api/intraday/triggers, /api/intraday/pdt, DELETE /api/portfolios/{id}/intraday-triggers
  (modificati) portfolios/models.py, engine/models.py, engine/service.py, risk/models.py, risk/checks.py,
  brokers/models.py, common/errors.py, models.py, signals/service.py, paperclip/issues.py,
  positions/protection.py, positions/open_view.py, admin/schemas.py, admin/routes_portfolios.py,
  admin/routes_orders.py, admin/routes_risk.py, admin/routes_signals.py, api/main.py, worker/main.py
backend/migrations/versions/0046_intraday.py
backend/tests/unit/test_mcp_catalog_frozen.py, test_intraday_calendar.py, test_intraday_pdt_count.py,
  test_intraday_wake_text.py, test_intraday_day_close_price.py, test_paperclip_issues.py
backend/tests/integration/test_intraday_schema.py, test_intraday_day_positions.py, test_intraday_pdt_check.py,
  test_intraday_pdt_reader.py, test_intraday_watch.py, test_intraday_wake.py, test_intraday_day_close.py,
  test_intraday_job.py, test_admin_intraday.py, test_intraday_history.py
web/src/api/types.ts, web/src/api/hooks/intraday.ts, web/src/pages/PortfolioDetailSettings.tsx,
  web/src/components/RiskProfileCard.tsx, web/src/pages/Signals.tsx, web/src/pages/Positions.tsx,
  web/src/components/OrderForm.tsx (+ test .test.tsx accanto)
docs/readiness/2026-12-intraday-prova-alpaca.md, docs/readiness/2026-12-campagna-prontezza.md
```

## Ordine e proprietà dei file

| Ondata | Task | Note |
|---|---|---|
| A | 1 → 2 | guardia MCP prima di tutto; poi schema (prerequisito di tutto) |
| B | 3 ∥ 7 ∥ 14 | 3: `intraday/calendar.py`; 7: `paperclip/issues.py`, `positions/protection.py`; 14: solo test di storico |
| C | 4 → 5 → 6 | `intraday/{day_positions,pdt,pdt_reader}.py`, `risk/checks.py`, `engine/service.py` (day_trade) |
| D | 8 → 9 | `signals/service.py`, `intraday/{watch,wake}.py` |
| E | 10 | `intraday/day_close.py`, `engine/service.py` (`close_position` con limite) — dopo 5 (stesso file) |
| F | 11 | `intraday/{job,summary}.py`, `worker/main.py` |
| G | 12 → 13 | API admin, poi `web/` |
| H | 15 → 16 | verifica finale e messa in linea spenta; manuale della prova reale |

---

### Task 1: Guardia di non regressione MCP

**Files:**
- Create: `tests/unit/test_mcp_catalog_frozen.py`
- Modify: `tests/integration/test_mcp_portfolio.py`, `tests/integration/test_mcp_orders.py` (solo nuove asserzioni sulle chiavi)

**Interfaces — Produces:** `FROZEN_FINGERPRINT = "fa77dcbd98f5c075a88b8cc8e1f902fb2677674ce2bbfc931a19d393593b71b0"`; nessun codice di prodotto.

- [ ] **Step 1: verificare la base.** Sul commit di partenza del ramo, prima di ogni modifica:
  `uv run python -c "import asyncio; from smtrading.mcp.catalog import tool_catalog_fingerprint as f; print(asyncio.run(f()))"` → deve stampare il valore qui sopra. Se `main` è andato avanti e il valore è diverso, usare il nuovo valore (calcolato **prima** di ogni modifica di questo piano) e annotarlo nel report.
- [ ] **Step 2: test.**
  - `test_tool_catalog_unchanged_by_intraday`: `assert await catalog_fingerprint(build_mcp()) == FROZEN_FINGERPRINT`, con messaggio «il catalogo MCP è cambiato: durante la campagna gli strumenti non devono cambiare».
  - In `test_mcp_portfolio.py`, `test_list_portfolios_keys_frozen`: l'insieme delle chiavi di ogni voce è esattamente `{"id","name","code","base_currency","status","in_totals","risk_profile","permission"} | chiavi di _mode_field` (copiare l'insieme osservato oggi) e non contiene nessuna chiave che inizia con `intraday` o `day_`.
  - In `test_mcp_orders.py`, `test_order_dto_has_no_day_trade`: l'ordine restituito da `place_order` via MCP non ha la chiave `day_trade`; le chiavi sono esattamente `ORDER_FIELDS`.
- [ ] **Step 3: eseguire** `uv run pytest -q -p no:cacheprovider tests/unit/test_mcp_catalog_frozen.py tests/integration/test_mcp_portfolio.py tests/integration/test_mcp_orders.py` → PASS (sulla base passano già: sono guardie).
- [ ] **Step 4: commit** `test(mcp): freeze the tool catalog and DTO keys for the intraday work`.

---

### Task 2: Schema, codice d'errore, `day_trade` sugli ordini

**Files:**
- Create: `migrations/versions/0046_intraday.py`, `intraday/__init__.py`, `intraday/models.py`
- Modify: `portfolios/models.py`, `engine/models.py`, `risk/models.py`, `brokers/models.py`, `common/errors.py`, `models.py` (registro), `engine/service.py` (`PlaceOrderRequest.day_trade`, `_persist`, `_same_params`)
- Test: `tests/integration/test_migrations.py` (drift esistente), `tests/integration/test_intraday_schema.py`

**Interfaces — Produces:**
- `Portfolio`: `intraday_enabled: bool` (`server_default false`); `intraday_timeframes: list[str]` JSONB not null `server_default '["15m","1h"]'`; `intraday_wake_agent_id: UUID|None` FK `agents.id` `ON DELETE SET NULL`; `day_close_minutes: int` not null `server_default 10`, CHECK `2 <= x <= 60`; `max_day_trades_5d: int|None`, CHECK `x >= 0`; `intraday_max_wakes_per_hour: int` not null `server_default 6`, CHECK `1 <= x <= 60`.
- `Order.day_trade: bool` default false, `server_default false`. `PlaceOrderRequest.day_trade: bool = False` copiato su `Order` in `_persist`; `_same_params` confronta anche `day_trade`.
- `RiskProfile.day_close_limit_margin_pct: Decimal` not null `server_default 0.5`, CHECK `0 < x <= 5`.
- `BrokerLink.pdt_state: dict|None` JSONB; `BrokerLink.pdt_state_at: datetime|None`.
- `IntradayTrigger` (`intraday_triggers`): `id BIGINT identity; created_at timestamptz server_default now(); portfolio_id UUID FK portfolios ON DELETE CASCADE; strategy_ref String(128); strategy_name String(200); symbol String(32); rule String(8)` CHECK `entry|exit`; `timeframe String(8); bar_close timestamptz; value NUM|None; threshold NUM|None; price NUM|None; rule_text String(200); outcome String(16)` CHECK su `pending, woken, telegram, capped, issue_open, no_agent, late, failed`; `issue_ident String(64)|None; error String(200)|None`. Indici `(portfolio_id, created_at)`, `(portfolio_id, symbol, id)`. Righe mai modificate se non `outcome`/`issue_ident`/`error` da `pending` all'esito finale; cancellate solo con il portafoglio o dall'endpoint di pulizia (Task 12).
- Costanti in `intraday/models.py`: `INTRADAY_TIMEFRAMES = ("5m", "15m", "1h")`, `DEFAULT_TIMEFRAMES = ("15m", "1h")`, gli 8 esiti come costanti stringa (`PENDING, WOKEN, TELEGRAM, CAPPED, ISSUE_OPEN, NO_AGENT, LATE, FAILED`).
- `ErrorCode.DAY_TRADE_LIMIT = "DAY_TRADE_LIMIT"`.

- [ ] **Step 1: test falliti:**
  - `test_migration_matches_models` (drift, esistente) rosso finché modelli e migrazione non coincidono;
  - `test_intraday_defaults_are_off`: un portafoglio creato con `create_portfolio` ha `intraday_enabled is False`, `intraday_timeframes == ["15m","1h"]`, `day_close_minutes == 10`, `intraday_max_wakes_per_hour == 6`, `intraday_wake_agent_id is None`, `max_day_trades_5d is None`; un ordine piazzato senza `day_trade` ha `day_trade is False`; il profilo «Default» ha `day_close_limit_margin_pct == Decimal("0.5")`;
  - `test_day_trade_propagates_to_order`: `place_order(..., PlaceOrderRequest(..., day_trade=True))` → `order.day_trade is True`;
  - `test_client_order_id_reused_with_other_day_trade_is_invalid`: stesso `client_order_id`, `day_trade` diverso → `INVALID_ORDER`;
  - `test_intraday_checks`: `day_close_minutes=1` e `intraday_max_wakes_per_hour=0` violano i CHECK (IntegrityError); `rule='both'` su `intraday_triggers` viola il CHECK;
  - `test_downgrade_0046_and_back`: `alembic downgrade 0045` poi `upgrade head` senza errori.
- [ ] **Step 2:** eseguire i test → FAIL.
- [ ] **Step 3:** modelli, migrazione `0046_intraday.py` (`down_revision = "0045"`, il nome esatto della revisione 0045 si legge dal file), `PlaceOrderRequest.day_trade`, `ErrorCode`.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_migrations.py tests/integration/test_intraday_schema.py tests/integration/test_order_service.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): schema behind intraday_enabled (off by default)`.

---

### Task 3: Calendario di giornata (puro)

**Files:**
- Create: `intraday/calendar.py`
- Test: `tests/unit/test_intraday_calendar.py`

**Interfaces — Consumes:** `instruments.market_hours.session_on, local_date, CALENDARS, _calendar` (fuso dell'exchange: `_calendar(CALENDARS[m]).tz`).
**Produces:**
- `BAR_DELAY = timedelta(seconds=60)` (la barra è disponibile dal fornitore).
- `def session_window(market: str, now: datetime) -> tuple[datetime, datetime] | None` — (apertura, chiusura) UTC della seduta della data locale di `now`; None senza seduta o per mercati senza calendario (CRYPTO, FX: niente giornata in questo piano).
- `def bar_closes(market: str, tf: Timeframe, day: date) -> list[datetime]` — vedi algoritmo.
- `def due_bar_close(market: str, tf: Timeframe, now: datetime, delay: timedelta = BAR_DELAY) -> datetime | None` — l'ultima chiusura `c` della seduta di oggi con `c + delay <= now < chiusura della seduta`; None altrimenti (a mercato chiuso, festività, prima della prima chiusura).
- `def day_close_at(market: str, now: datetime, minutes: int) -> datetime | None` — chiusura della seduta di oggi meno `minutes`.
- `def last_sessions(market: str, now: datetime, n: int = 5) -> list[date]` — le ultime `n` date di seduta fino a oggi compreso (se oggi è seduta), dalla più vecchia.
- `def session_after(market: str, day: date, n: int) -> date` — l'`n`-esima seduta dopo `day`.

Algoritmo di `bar_closes` (le barre Alpaca sono allineate all'orologio: 1h alle ore piene, 15m ai quarti; la barra 9:00-10:00 ET chiude alle 10:00):

```python
s = session_on(market, day)            # chiusure manuali e mezze giornate incluse
if s is None: return []
open_, close, _ = s
tz = exchange tz; step = DURATION[tf]
local_open = open_.astimezone(tz)
midnight = local_open.replace(hour=0, minute=0, second=0, microsecond=0)
k = (local_open - midnight) // step + 1   # primo multiplo strettamente dopo l'apertura
out, t = [], midnight + k * step          # aritmetica a orologio locale (DST corretto)
while t.astimezone(UTC) <= close:
    out.append(t.astimezone(UTC)); t += step
return out
```

- [ ] **Step 1: test falliti** (date reali del calendario NYSE):
  - `test_thanksgiving_has_no_bar_closes`: `bar_closes("NYSE", m15, date(2026,11,26)) == []`, `due_bar_close("NYSE", m15, 2026-11-26 16:00Z) is None`;
  - `test_half_day_last_close_is_13_et`: `bar_closes("NYSE", m15, date(2026,11,27))[0] == 2026-11-27 14:45Z` e `[-1] == 18:00Z` (13:00 ET), `len == 14`; per `h1` `[0] == 15:00Z` (10:00 ET), `[-1] == 18:00Z`;
  - `test_regular_day_counts`: 2026-12-01 → `m15` 26 chiusure (14:45Z … 21:00Z), `h1` 7 (15:00Z … 21:00Z), `m5` 78;
  - `test_dst_week`: 2026-03-09 (dopo il cambio USA dell'8/3) `h1[0] == 14:00Z`;
  - `test_due_bar_close_waits_for_delay`: alle 15:00:59Z del 2026-12-01 → `14:45Z`; alle 15:01:00Z → `15:00Z`; alle 21:00:30Z → None (seduta chiusa: l'ultima barra non si valuta);
  - `test_manual_early_close`: con `set_closures([Closure("NYSE", date(2026,12,2), EARLY_CLOSE, time(12,0))])` l'ultima chiusura `m15` è 17:00Z e `day_close_at("NYSE", 2026-12-02 15:00Z, 10) == 16:50Z`; ripulire le chiusure a fine test;
  - `test_last_sessions_skips_holiday`: `last_sessions("NYSE", 2026-11-30 15:00Z) == [11-23, 11-24, 11-25, 11-27, 11-30]`; `session_after("NYSE", date(2026,11,25), 1) == date(2026,11,27)`;
  - `test_crypto_has_no_window`: `session_window("CRYPTO", ...) is None`, `bar_closes("CRYPTO", m15, ...) == []`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare le sei funzioni in `intraday/calendar.py`.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_calendar.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): bar-close and session calendar`.

---

### Task 4: Posizioni di giornata e conteggio PDT

**Files:**
- Create: `intraday/day_positions.py`, `intraday/pdt.py`
- Test: `tests/unit/test_intraday_pdt_count.py`, `tests/integration/test_intraday_day_positions.py`

**Interfaces — Consumes:** `intraday.calendar.session_window, last_sessions, session_after`; `Fill`, `Order`, `Position`, `Instrument`, `Portfolio`, `BrokerLink`.
**Produces:**
- `async def day_quantities(session, portfolio_id: UUID, now: datetime) -> dict[str, Decimal]` — per simbolo (solo mercati con calendario) `min(qty della posizione long, max(0, comprato oggi con ordini day_trade − venduto oggi))`, solo valori > 0. «Oggi» = `Fill.created_at` nella `session_window` del mercato dello strumento. Short esclusi (fuori spec).
- `@dataclass(frozen=True) class FillRow: order_id: UUID; symbol: str; side: OrderSide; qty: Decimal; day: date; ts: datetime`.
- `def count_day_trades(fills: Iterable[FillRow]) -> dict[date, int]` — algoritmo sotto.
- `PDT_KINDS = frozenset({"alpaca_paper", "alpaca_live"})`, `PDT_MARKET = "NYSE"`, `PDT_MAX = 3`, `PDT_SESSIONS = 5`, `PDT_EQUITY_MIN = Decimal(25000)`.
- `def pdt_applies(kind: str, state: dict | None) -> bool` — False se `kind not in PDT_KINDS`; True se `state` è None (prudente: mai letto); altrimenti `multiplier > 1 and min(equity, last_equity) < PDT_EQUITY_MIN` (valori assenti ignorati nel minimo; tutti assenti → True).
- `@dataclass(frozen=True) class PdtBudget: applies: bool; limit: int | None; used: int; pending: int; remaining: int | None; frees_on: date | None; broker_count: int | None; mismatch: bool`.
- `async def link_budget(session, link_id: UUID, now: datetime) -> PdtBudget` — riempimenti di **tutti** gli ordini con `orders.broker_link_id == link_id` su strumenti `us_equity` nelle `last_sessions(PDT_MARKET, now, 5)`; `limit = PDT_MAX` se applica, altrimenti None; `pending` = simboli con quantità di giornata aperta su portafogli del collegamento + simboli con un acquisto `day_trade` a riposo (`FILLABLE_STATUSES`, non reduce-only) e nessuna posizione di giornata; `remaining = limit - used - pending` (mai sotto 0) o None; `frees_on` = `session_after(d, 5)` della seduta più vecchia della finestra con operazioni; `broker_count` = `pdt_state["daytrade_count"]`, `mismatch = broker_count is not None and broker_count != used`.
- `async def entry_budget(session, portfolio: Portfolio, symbol: str, now: datetime) -> PdtBudget` — combina il budget del collegamento (se `broker_link_id` e tipo in `PDT_KINDS`) con il tetto proprio `max_day_trades_5d` (contato sui riempimenti del solo portafoglio): `remaining = min` dei due; un `symbol` che ha già una posizione di giornata o un acquisto `day_trade` a riposo non aggiunge un'operazione futura (lo si toglie da `pending`). Senza collegamento PDT e senza tetto proprio: `remaining=None`.

Algoritmo di `count_day_trades` (prudente: una vendita che chiude quantità comprata nella stessa seduta conta come un'operazione per ogni **ordine** di vendita; acquisto-acquisto-vendita = 1, acquisto-vendita-acquisto-vendita = 2, acquisto-vendita-vendita = 2):

```python
out = defaultdict(int)
for (day, symbol), rows in groupby(sorted(fills, key=(day, symbol, ts)), key=(day, symbol)):
    bought, counted = Decimal(0), set()
    for f in rows:
        if f.side == buy: bought += f.qty; continue
        matched = min(f.qty, bought)
        if matched > 0:
            bought -= matched
            if f.order_id not in counted: counted.add(f.order_id); out[day] += 1
return dict(out)
```

- [ ] **Step 1: test falliti:**
  - unit: `test_buy_buy_sell_is_one`, `test_buy_sell_buy_sell_is_two`, `test_buy_sell_sell_is_two`, `test_two_fills_of_one_sell_order_count_once`, `test_overnight_shares_sold_today_is_not_a_day_trade` (solo vendita oggi → 0), `test_days_are_separate` (acquisto lunedì, vendita martedì → 0);
  - unit: `test_pdt_applies`: `("t212_demo", None) → False`; `("alpaca_paper", None) → True`; `multiplier "2", equity "24999.99" → True`; `multiplier "1", equity "1000" → False` (conto contante); `multiplier "4", equity "30000", last_equity "24000" → True`;
  - integration: `test_day_quantities_mixed_position` (60 comprati ieri, 40 oggi con `day_trade`, nessuna vendita → `{"AAPL": 40}`; vendita oggi di 50 → `{}`); `test_day_quantities_ignores_non_day_trade_buys`;
  - integration: `test_link_budget_counts_all_portfolios_of_the_link` (2 portafogli sullo stesso collegamento, uno giornaliero: 1 operazione ciascuno → `used == 2`, `remaining == 1`), `test_link_budget_window_with_holiday` (operazioni il 23/11 e il 30/11: al 30/11 `used == 2`; al 1/12 il 23/11 esce → `used == 1`; `frees_on == date(2026,12,1)` al 30/11), `test_pending_counts_open_day_positions_and_resting_entries`, `test_mismatch_with_broker_count` (`pdt_state={"daytrade_count": 2}`, nostro 1 → `mismatch is True`);
  - integration: `test_entry_budget_own_cap` (`max_day_trades_5d=1`, simulato senza collegamento, 1 operazione → `remaining == 0`), `test_entry_budget_same_symbol_adds_nothing`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare `day_positions.py` e `pdt.py` (query: `Fill` ⋈ `Order` ⋈ `Instrument`, filtro su `Fill.created_at >= apertura della seduta più vecchia della finestra`).
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_pdt_count.py tests/integration/test_intraday_day_positions.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): day positions and the 5-session day-trade count`.

---

### Task 5: Controllo PDT prima dell'invio e avviso sulle vendite

**Files:**
- Modify: `risk/checks.py` (`pre_trade_check`, `_run`), `engine/service.py` (`_run_check` passa `day_trade`; `_place` chiama l'avviso), `intraday/pdt.py` (`warn_day_trade_sell`)
- Test: `tests/integration/test_intraday_pdt_check.py`

**Interfaces — Consumes:** `entry_budget`, `link_budget`, `count_day_trades` (Task 4).
**Produces:**
- `pre_trade_check(..., day_trade: bool = False)`; in `_run`, dopo il controllo di potere d'acquisto (passo 10): se `side == buy and day_trade and portfolio.intraday_enabled` e `entry_budget(...).remaining` non è None e `<= 0` → `_Reject(ErrorCode.DAY_TRADE_LIMIT, msg, {"used", "limit", "remaining": 0, "frees_on"})`. Testo: `"Operazioni di giornata esaurite: {used} usate su {limit} nelle ultime 5 sedute (più {pending} già aperte), ne restano 0; la prossima si libera il {gg/mm}"` (senza `frees_on`: «quando una delle aperte si chiude senza operazione»).
- `async def warn_day_trade_sell(session, order: Order, now: datetime) -> None` — per un ordine di vendita non rifiutato di un portafoglio **senza** `intraday_enabled`, collegato a un link in `PDT_KINDS` che ha almeno un portafoglio non archiviato con `intraday_enabled`: se la vendita chiuderebbe quantità comprata nella seduta e `used + 1 >= PDT_MAX` con `pdt_applies` → un `enqueue(source="system", category="system", level="warning")` «Attenzione regola USA sui conti piccoli: la vendita di {symbol} su {portafoglio} sarebbe l'operazione di giornata n. {used+1} su {PDT_MAX} nelle ultime 5 sedute del collegamento {link}». Esce subito (nessuna query oltre la prima) se il portafoglio non ha `broker_link_id`.
- Chiamata in `engine/service._place` dopo `_activate`, solo con `order.side == sell` e stato non `rejected`.

- [ ] **Step 1: test falliti:**
  - `test_fourth_day_trade_entry_refused`: portafoglio `intraday_enabled`, collegamento `alpaca_paper` con `pdt_state` sotto 25.000 $, 3 operazioni nelle 5 sedute → acquisto `day_trade=True` rifiutato `DAY_TRADE_LIMIT`, `details["remaining"] == 0`, testo contiene «ne restano 0» e la data di `frees_on`;
  - `test_entry_refused_when_pending_fill_the_budget` (Review Focus 5): 2 usate, una posizione di giornata aperta su MSFT → acquisto `day_trade` su AAPL rifiutato; un secondo acquisto `day_trade` su MSFT accettato;
  - `test_sells_never_refused`: stesso scenario, vendita reduce-only della posizione → accettata;
  - `test_non_day_trade_buy_not_checked`: `day_trade=False` → accettato;
  - `test_off_portfolio_never_refused` (non regressione): stesso collegamento, portafoglio senza `intraday_enabled`, acquisto `day_trade=True` → accettato;
  - `test_cash_account_not_limited`: `pdt_state={"multiplier": "1", ...}` → accettato;
  - `test_off_portfolio_sell_warns_on_shared_link`: portafoglio giornaliero sullo stesso collegamento di uno di giornata, 2 operazioni, acquisto e vendita oggi → una riga `telegram_outbox` `category="system"` col testo «operazione di giornata n. 3 su 3»;
  - `test_no_warning_without_intraday_on_link` (non regressione): stesso scenario senza portafogli di giornata sul collegamento → nessuna riga outbox;
  - `test_preview_reports_day_trade_limit`: `preview_order` con il budget finito → `ok=False`, `code == "DAY_TRADE_LIMIT"`, nessuna riga outbox.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_pdt_check.py tests/integration/test_risk_checks.py tests/integration/test_order_service.py tests/integration/test_admin_orders.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): refuse day-trade entries when the PDT budget is spent`.

---

### Task 6: Lettura PDT dal broker e confronto

**Files:**
- Create: `intraday/pdt_reader.py`
- Test: `tests/integration/test_intraday_pdt_reader.py`

**Interfaces — Consumes:** `brokers.service.open_adapter`, `BrokerAdapter.get_account() -> AccountSnapshot` (`raw` è il JSON di `GET /v2/account`), `link_budget`, `jobs.runs.run_job_once`, `telegram.outbox.enqueue`.
**Produces:**
- `PDT_READ_INTERVAL_S = 300`.
- `async def refresh_pdt_states(sessionmaker, http, now: datetime) -> int` — numero di collegamenti letti. Collegamenti `enabled` con `kind in PDT_KINDS` e almeno un portafoglio non archiviato con `intraday_enabled`; lettura dell'account senza transazione aperta; salva `pdt_state = {"equity", "last_equity", "multiplier", "daytrade_count", "pattern_day_trader"}` (stringhe/int/bool da `raw`, nient'altro: niente ID conto) e `pdt_state_at`; se `link_budget(...).used != daytrade_count` → `log.warning` (senza ID conto) e, una volta per `run_job_once("intraday_pdt_mismatch", f"{link_id}:{data}:{nostro}:{broker}")`, Telegram «Conteggio operazioni di giornata diverso dal broker su {nome collegamento}: SMTrading {nostro}, Alpaca {broker}». Con `pattern_day_trader == True` un avviso «conto segnato come day trader dal broker» una volta per seduta. Un errore di lettura si registra nel log e il collegamento si riprova al giro dopo.

- [ ] **Step 1: test falliti** (respx sul `GET /v2/account` del fake Alpaca paper, `assert_all_mocked=True`):
  - `test_reads_only_links_with_intraday_portfolios`: due collegamenti, solo uno con un portafoglio di giornata → una sola richiesta, `pdt_state` salvato solo lì, con le 5 chiavi esatte;
  - `test_mismatch_alert_once`: broker 2, nostro 1 → una riga outbox; secondo giro uguale → nessuna nuova riga;
  - `test_read_failure_keeps_old_state`: HTTP 500 → `pdt_state` invariato, nessuna eccezione;
  - `test_no_secret_in_state_or_logs`: `caplog` e `pdt_state` non contengono la key ID canarino né l'ID conto.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_pdt_reader.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): read the broker's day-trade count and compare it`.

---

### Task 7: Scrittore di compiti Paperclip generalizzato al destinatario

**Files:**
- Modify: `paperclip/issues.py`, `positions/protection.py`
- Create: `tests/unit/test_paperclip_issues.py`
- Test: `tests/integration/test_protection_alarm.py` (invariato, deve restare verde)

**Interfaces — Produces:**
- `find_agent_id(agents: list[dict], name: str) -> str | None` — senza default (il default `RISK_MANAGER` passa al chiamante).
- `async def resolve_agent(client: PaperclipIssues, name: str) -> str` — l'id, altrimenti `PaperclipError(f"agente «{name}» non trovato")`.
- `PaperclipIssues.open_identifiers(self) -> set[str]` — identificatori (altrimenti id) dei compiti non chiusi (`CLOSED_STATUSES` esclusi), un solo `GET .../issues`.
- `ProtectionAlarm._create_issues` usa `resolve_agent(client, RISK_MANAGER)`: stesso comportamento e stesso testo d'errore di oggi.

- [ ] **Step 1: test falliti:** `test_resolve_agent_by_any_name` (nomi con maiuscole/spazi diversi), `test_resolve_agent_missing_message` («agente «X» non trovato»), `test_open_identifiers_skips_done_and_cancelled`, `test_transport_still_refuses_other_writes` (PATCH su un compito → `RuntimeError(WRITE_REFUSED)` prima della rete).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare; nessuna modifica al testo dei compiti «PROTEGGI».
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_paperclip_issues.py tests/integration/test_protection_alarm.py tests/integration/test_admin_paperclip.py` → PASS.
- [ ] **Step 5: commit** `refactor(paperclip): issue writer resolves any recipient agent`.

---

### Task 8: Segnali con selettore e rilevazione degli scatti

**Files:**
- Modify: `signals/service.py`
- Create: `intraday/watch.py`
- Test: `tests/integration/test_signals.py` (invariato + 1 test), `tests/integration/test_intraday_watch.py`

**Interfaces — Consumes:** `strategy_signals`, `_Approved`, `due_bar_close`, `day_close_at`, Redis.
**Produces:**
- `signals.service.Approved = _Approved` (alias pubblico).
- `strategy_signals(session, router, now=None, *, select: Callable[[Approved], Approved | None] | None = None)` — con `select` ogni approvata passa dal selettore prima di leggere i dati (None = esclusa); senza, identico a oggi. Sui timeframe brevi non c'è barra in formazione (`forming.FORMING_TIMEFRAMES`): entrata e uscita sono già sulle barre chiuse.
- `@dataclass(frozen=True) class Trigger: portfolio_id: UUID; ref: str; name: str; symbol: str; rule: Literal["entry","exit"]; timeframe: str; bar_close: datetime; value: Decimal | None; threshold: Decimal | None; price: Decimal | None; rule_text: str; late: bool`.
- `def intraday_selector(portfolios: dict[UUID, Portfolio], tf: Timeframe) -> Callable[[Approved], Approved | None]` — tiene l'approvata solo se il suo timeframe è `tf`, `tf.value` è in `INTRADAY_TIMEFRAMES`, e restringe `portfolios` a quelli in `portfolios` (di giornata, non archiviati) con `tf.value in p.intraday_timeframes`; nessuno → None.
- `STATE_KEY = "intraday:sig:{portfolio_id}:{ref}:{symbol}:{rule}"`, `STATE_TTL_S = 7 * 86400`.
- `async def evaluate_close(session, router, redis, tf: Timeframe, market: str, bar_close: datetime, now: datetime) -> list[Trigger]` — portafogli di giornata non archiviati; `strategy_signals(..., select=intraday_selector(...))`; solo strategie con stato `ok` e simboli il cui strumento è nel `market`; valore attuale: entrata = `triggered_closed and position is None and not open_entry`, uscita = `position is not None and exit["triggered"]`; scatto solo se il valore precedente in Redis è `"0"`; chiave mancante → si scrive il valore attuale **senza** scatto; si scrive sempre il valore attuale con `STATE_TTL_S`. `late = rule == "entry" and bar_close >= day_close_at(market, now, p.day_close_minutes)`. `rule_text`: entrata `f"{indicator} {direction} {threshold}"` in italiano («sotto», «sopra», «incrocia al rialzo»), uscita `exit["rule"]`.

- [ ] **Step 1: test falliti** (fake router con barre 15m come in `test_signals.py`, `fakeredis` o la Redis dei test):
  - `test_select_none_is_unchanged` (non regressione, in `test_signals.py`): `strategy_signals(..., select=None)` uguale all'uscita di oggi su tutti i fixture esistenti (confronto dataclass);
  - `test_selector_keeps_only_intraday_portfolios`: strategia 15m su due portafogli, uno solo di giornata → una sola `StrategySignal`; strategia 1d → esclusa; portafoglio di giornata con `intraday_timeframes=["1h"]` e strategia 15m → esclusa;
  - `test_entry_fires_once`: primo giro con chiave assente e RSI sotto soglia → nessuno scatto; giro con valore precedente `"0"` e RSI sotto → 1 `Trigger(rule="entry")`; giro dopo, ancora sotto → nessuno;
  - `test_no_entry_with_position_or_resting_entry`;
  - `test_exit_fires_with_position`: posizione aperta, regola d'uscita da falsa a vera → `Trigger(rule="exit")`, `rule_text == exit["rule"]`;
  - `test_late_entry_after_cutoff`: `bar_close` = 20:50Z del 2026-12-01 con `day_close_minutes=10` → `late is True`; uscita alla stessa ora → `late is False`;
  - `test_other_market_symbols_ignored`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_signals.py tests/integration/test_intraday_watch.py tests/integration/test_admin_signals.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): detect intraday signal triggers at bar close`.

---

### Task 9: Sveglia dell'agente (compito Paperclip o Telegram)

**Files:**
- Create: `intraday/wake.py`
- Test: `tests/unit/test_intraday_wake_text.py`, `tests/integration/test_intraday_wake.py`

**Interfaces — Consumes:** `Trigger` (Task 8), `IntradayTrigger` e costanti (Task 2), `PaperclipIssues`, `resolve_agent`, `open_identifiers`, `open_issue_with_title` (Task 7), `agents.access.portfolios_with_agents`, `AgentPortfolio`, `paperclip.config.load_config`, `enqueue`.
**Produces:**
- `def wake_title(t: Trigger, portfolio: Portfolio) -> str` → `f"SEGNALE · {portfolio.code or portfolio.name} · {t.symbol} · {'entrata' if t.rule == 'entry' else 'uscita'}"`.
- `def wake_text(t: Trigger, portfolio: Portfolio, zone: ZoneInfo) -> str` — righe: strategia (`name`, `ref`), timeframe, regola (`rule_text`), valore e soglia (`_it` con virgola), prezzo, ora della barra nel fuso dell'utente, ultima riga «Decidi tu se e come operare: SMTrading non ha inviato ordini.». Nessun nome fisso.
- `class Waker: __init__(self, sessionmaker, *, issues_factory: IssuesFactory | None = None, clock=None, tz: str | None = None)`; `async def handle(self, triggers: list[Trigger]) -> None`.
  1. In una transazione: una riga `IntradayTrigger` `pending` per scatto; esito deciso subito, nell'ordine: `late` → `LATE`; portafoglio senza accesso agenti → `TELEGRAM` (Telegram «system» con titolo e testo); `intraday_wake_agent_id` None, agente inattivo o senza `trade` sul portafoglio → `NO_AGENT` + Telegram con lo stesso testo e la riga «nessun agente da svegliare: imposta il destinatario nel portafoglio»; righe `WOKEN` del portafoglio negli ultimi 3600 s `>= intraday_max_wakes_per_hour` → `CAPPED`; il resto resta `pending` per Paperclip.
  2. Senza transazione aperta: Paperclip disattivato/incompleto → `FAILED` + Telegram con lo stesso testo; altrimenti `resolve_agent(client, agent.name)`, `open_identifiers()`; se l'ultimo `issue_ident` della stessa coppia (portafoglio, simbolo) è ancora aperto → `ISSUE_OPEN` con quell'identificatore; altrimenti `create_issue(title, description, assignee, priority="high")` → `WOKEN`; dopo un `timeout` → `open_issue_with_title(title)` e, se trovato, `WOKEN` con quello; errore → `FAILED`, `error` = messaggio breve, Telegram con lo stesso testo.
  3. In una transazione: aggiornamento di `outcome`, `issue_ident`, `error` delle righe `pending`.
  Un `pending` più vecchio di 10 minuti trovato all'avvio di `handle` si chiude con `open_issue_with_title` (trovato → `WOKEN`, altrimenti `FAILED`): nessun secondo compito.

- [ ] **Step 1: test falliti:**
  - unit: `test_title_uses_code_then_name`, `test_text_has_values_and_bar_time` (valore `Decimal("28.5")` → «28,5»; ora 14:45Z → «15:45» Europe/Rome), `test_text_has_no_fixed_names` (nessuna stringa di `docs/paperclip-agents/*.md` titoli di agente nel modulo: `"Portfolio Manager" not in inspect.getsource(wake)`);
  - integration (fake `issues_factory` come in `test_protection_alarm.py`): `test_woken_creates_one_issue_for_the_configured_agent` (assegnatario = id Paperclip del nome dell'agente configurato), `test_one_open_issue_per_portfolio_symbol` (secondo scatto su AAPL con compito aperto → `ISSUE_OPEN`, nessuna seconda `create_issue`; dopo che il compito è `done` → nuovo compito), `test_cap_per_hour` (`intraday_max_wakes_per_hour=2`, tre simboli → 2 `WOKEN`, 1 `CAPPED`), `test_portfolio_without_agents_goes_to_telegram` (nessun compito, una riga outbox `system` con lo stesso titolo), `test_agent_downgraded_to_read_is_no_agent` (Review Focus 4), `test_paperclip_disabled_is_failed_with_telegram`, `test_timeout_adopts_existing_issue`, `test_stale_pending_adopted_not_duplicated` (Review Focus 3), `test_late_entry_recorded_without_wake`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_wake_text.py tests/integration/test_intraday_wake.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): wake the portfolio's agent on a trigger`.

---

### Task 10: Rete di sicurezza di fine seduta

**Files:**
- Modify: `engine/service.py` (`_close`, `close_position`)
- Create: `intraday/day_close.py`
- Test: `tests/unit/test_intraday_day_close_price.py`, `tests/integration/test_intraday_day_close.py`

**Interfaces — Consumes:** `day_quantities` (Task 4), `day_close_at`, `session_window` (Task 3), `marketdata.quality.is_wide`, `effective_risk_profile`, `run_job_once`, `enqueue`.
**Produces:**
- `close_position(..., limit_price: Decimal | None = None, client_order_id: str | None = None)` e lo stesso in `_close`: con `limit_price` l'ordine reduce-only è `limit` a quel prezzo, altrimenti `market` come oggi; `client_order_id` passato alla richiesta (idempotenza dell'engine). Default invariati.
- `def close_limit_price(quote: Quote, margin_pct: Decimal) -> tuple[bool, Decimal | None]` — (forbice larga, prezzo): stretta → `(False, None)` (a mercato); larga con `last` → `(True, last * (1 - margin_pct/100))` arrotondato al tick con `instruments.service.validate_price`/arrotondamento per difetto; larga senza `last` → `(True, None)`.
- `DAY_CLOSE_JOB = "intraday_day_close"`, `DAY_CLOSE_CHECK_JOB = "intraday_day_close_check"`, `CHECK_BEFORE_END = timedelta(minutes=1)`.
- `class DayCloser: __init__(self, sessionmaker, qs, redis, *, clock=None, tz=None)`; `async def tick(self, now: datetime) -> None`:
  - per ogni portafoglio di giornata non archiviato e ogni simbolo di `day_quantities` il cui mercato ha `day_close_at(market, now, p.day_close_minutes) <= now < chiusura` e `market_status(...).is_open`: `run_job_once(DAY_CLOSE_JOB, f"{pid}:{symbol}:{data}")` → quote → `close_limit_price(quote, profilo.day_close_limit_margin_pct or 0.5)` → `close_position(qty=quantità di giornata, source="system", agent_id=None, limit_price=..., client_order_id=f"smt-dayclose-{data:%Y%m%d}-{symbol}")`. Telegram per ogni chiusura: «Chiusura di sicurezza · {nome} · {simbolo}: {qty} in vendita {a mercato | con limite {prezzo} (forbice larga)}, {m} minuti prima della fine seduta. Motivo: posizione di giornata ancora aperta.» più, se resta quantità di ieri, «Restano {n} comprati prima di oggi: riproteggili (stop e obiettivo del simbolo annullati).» Forbice larga senza ultimo scambio: nessun ordine, Telegram «chiusura di sicurezza impossibile: nessun prezzo affidabile».
  - a `chiusura - CHECK_BEFORE_END <= now < chiusura`: `run_job_once(DAY_CLOSE_CHECK_JOB, ...)`; quantità di giornata ancora > 0 → Telegram «chiusura di sicurezza non eseguita · {nome} · {simbolo}: {qty} ancora aperti a 1 minuto dalla fine». Mai un secondo ordine.
  - errore SMTError dell'engine (ordine rifiutato) → Telegram con codice e messaggio.

- [ ] **Step 1: test falliti:**
  - unit: `test_tight_quote_is_market` (bid 100, ask 100,5 → `(False, None)`), `test_wide_quote_uses_last_minus_margin` (bid 95, ask 105, last 100, margine 0,5 → `(True, Decimal("99.50"))`), `test_wide_without_last_is_none`;
  - integration: `test_closes_day_qty_at_cutoff` (posizione di giornata 10 AAPL, 2026-12-01 20:50:30Z → un ordine reduce-only `source="system"`, `qty=10`, `client_order_id="smt-dayclose-20261201-AAPL"`, una riga outbox), `test_not_before_cutoff` (20:49Z → nessun ordine), `test_never_with_market_closed` (Thanksgiving 26/11 e 21:00:30Z del 1/12 → nessun ordine), `test_half_day_cutoff` (27/11: 17:50Z chiude, 20:50Z no), `test_mixed_position_sells_only_day_qty` (Review Focus 1: 60 di ieri + 40 oggi → `qty=40`, testo con «Restano 60»), `test_wide_spread_limit_order`, `test_unfilled_alert_at_one_minute` (Review Focus 2: ordine limite non eseguito → alle 20:59:10Z una riga «non eseguita», nessun secondo ordine), `test_run_once_per_session` (due tick nella finestra → un ordine), `test_off_portfolio_untouched` (non regressione: portafoglio senza `intraday_enabled` con acquisto `day_trade` → nessun ordine), `test_close_position_defaults_unchanged` (non regressione: `close_position` senza nuovi argomenti → ordine `market`, `client_order_id is None`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_day_close_price.py tests/integration/test_intraday_day_close.py tests/integration/test_order_service.py tests/integration/test_synthetic_orders.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): safety close of day positions before the session end`.

---

### Task 11: Job del worker e riepilogo di giornata

**Files:**
- Create: `intraday/job.py`, `intraday/summary.py`
- Modify: `worker/main.py`
- Test: `tests/integration/test_intraday_job.py`

**Interfaces — Consumes:** `evaluate_close` (Task 8), `Waker` (Task 9), `DayCloser` (Task 10), `refresh_pdt_states` (Task 6), `due_bar_close`, `session_window` (Task 3), `approved_strategies`, `run_job_once`, `job_tick`.
**Produces:**
- `TICK_S = 15`, `BAR_JOB = "intraday_bar"`, `SUMMARY_JOB = "intraday_summary"`, `SUMMARY_AFTER_CLOSE = timedelta(minutes=5)`.
- `class IntradayJob: __init__(self, sessionmaker, router, redis, http, *, issues_factory=None, clock=None, tz=None, tick_s: float = TICK_S)`; `async def run(self) -> None` (ciclo con `job_tick()`, eccezioni registrate e ciclo che continua, come `ProtectionAlarm.run`); `async def tick(self) -> None`:
  1. una query: portafogli non archiviati con `intraday_enabled`; nessuno → ritorno **senza** toccare Redis, router, Paperclip o broker;
  2. coppie (timeframe, mercato): timeframe dall'unione di `intraday_timeframes`, mercati degli strumenti dei simboli delle approvate su quei portafogli (`approved_strategies`, nessun dato di mercato); per ognuna `c = due_bar_close(...)`; `c` non None e `run_job_once(BAR_JOB, f"{tf}:{market}:{c.isoformat()}")` (commit prima del calcolo) → `evaluate_close` → `Waker.handle`;
  3. `DayCloser.tick(now)`;
  4. `refresh_pdt_states` ogni `PDT_READ_INTERVAL_S` mentre la seduta `NYSE` è aperta, più una volta nei 10 minuti dopo la chiusura;
  5. a `chiusura + SUMMARY_AFTER_CLOSE`, per portafoglio con scatti nella seduta: `run_job_once(SUMMARY_JOB, f"{pid}:{data}")` → Telegram `day_summary_text`.
- `async def day_summary_text(session, portfolio: Portfolio, day: date) -> str | None` — «Riepilogo di giornata · {nome} · {gg/mm}: {n} scatti — {woken} sveglie, {capped} saltati per tetto, {issue_open} con compito già aperto, {no_agent} senza agente, {late} dopo l'orario, {failed} non riusciti; operazioni di giornata {used}/{limit} (la prossima si libera il …)»; None senza scatti.
- `worker/main.py`: `supervise("intraday", lambda: gated(gate, lambda: IntradayJob(sessionmaker, router, redis, http).run()))`.

- [ ] **Step 1: test falliti:**
  - `test_idle_without_intraday_portfolios` (non regressione): con portafogli solo giornalieri, router e Redis finti che falliscono a ogni chiamata → `tick()` non solleva e non li chiama; nessuna riga in `job_runs`, `intraday_triggers`, `telegram_outbox`;
  - `test_bar_processed_once`: due `tick()` alle 15:01:05Z e 15:01:20Z → `evaluate_close` chiamato una volta per (15m, NYSE, 15:00Z);
  - `test_restart_does_not_reprocess` (Review Focus 3): nuovo `IntradayJob` sullo stesso database dopo il primo tick → nessuna nuova valutazione della stessa barra;
  - `test_summary_once_after_close`: scatti nella seduta → alle 21:05:30Z una riga outbox col riepilogo; secondo tick → nessuna;
  - `test_worker_registers_intraday_behind_gate`: `"intraday"` tra i job avviati da `worker.main.run` (come per gli altri job dietro `gated`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_job.py tests/unit/test_worker.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): worker job (bar closes, day close, PDT, summary)`.

---

### Task 12: API admin

**Files:**
- Create: `admin/routes_intraday.py`
- Modify: `admin/schemas.py` (`PortfolioPatchIn`, `RiskProfilePatchIn`), `admin/routes_portfolios.py` (`PORTFOLIO_FIELDS`, `patch_portfolio`, `_order_dto`), `admin/routes_orders.py` (`OrderIn.day_trade`), `admin/routes_risk.py`, `admin/routes_signals.py`, `positions/open_view.py`, `api/main.py` (router), `cli.py` (comandi `intraday`)
- Test: `tests/integration/test_admin_intraday.py`, `tests/integration/test_admin_portfolios.py`, `tests/integration/test_admin_orders.py`, `tests/integration/test_admin_signals.py`

**Interfaces — Consumes:** Task 2, 4, 8.
**Produces (solo API admin, mai MCP):**
- `PortfolioPatchIn`: `intraday_enabled: StrictBool | None`, `intraday_timeframes: list[str] | None` (sottoinsieme non vuoto di `INTRADAY_TIMEFRAMES`, senza doppioni), `intraday_wake_agent_id: uuid.UUID | None` (deve avere `permission == "trade"` sul portafoglio ed essere attivo, altrimenti `INVALID_ORDER` «l'agente da svegliare deve avere il permesso trade su questo portafoglio»; `null` lo toglie), `day_close_minutes: int | None` (2-60), `max_day_trades_5d: int | None` (0-100, `null` toglie), `intraday_max_wakes_per_hour: int | None` (1-60). Modifiche solo admin (`require_admin` come gli altri campi sensibili del PATCH); ogni modifica di questi campi → `write_audit(method="admin:portfolio.intraday", ...)`.
- `PORTFOLIO_FIELDS` + i sei campi; `_order_dto` + `day_trade` (`ORDER_FIELDS` MCP invariato).
- `OrderIn.day_trade: bool = False` → `PlaceOrderRequest.day_trade`; anche nell'anteprima.
- `RiskProfilePatchIn.day_close_limit_margin_pct: Num | None` (0 < x ≤ 5), nel DTO admin del profilo.
- `GET /api/intraday/triggers?portfolio_id=&limit=` (default 50, max 200) → `[{id, created_at, portfolio_id, portfolio_name, strategy_ref, strategy_name, symbol, rule, timeframe, bar_close, value, threshold, price, rule_text, outcome, issue_ident, error}]`, più recenti prima.
- `GET /api/intraday/pdt` → per collegamento con almeno un portafoglio: `{link_id, link_name, kind, applies, limit, used, pending, remaining, frees_on, broker_count, mismatch, pdt_state_at}` (da `link_budget`; nessun ID conto).
- `DELETE /api/portfolios/{id}/intraday-triggers` (admin) → `{deleted: n}`, audit `admin:intraday.triggers_delete` (pulizia di fine prova).
- `open_positions`: ogni riga ha `day_qty` (stringa) per i portafogli di giornata, `null` per gli altri (nessuna query in più per questi).
- `/api/signals`: per strategia `intraday: bool` (timeframe in `INTRADAY_TIMEFRAMES` e portafoglio `intraday_enabled`); `CACHE_KEY = "signals:v4"`.
- CLI per la strategia dell'esercitazione (le strategie oggi nascono solo dalle decisioni del CEO via MCP; il portale non le crea): `python -m smtrading.cli intraday drill-strategy --portfolio <id> --agent <nome> --ref <ref> --type <tipo> --params '<json>' --timeframe 15m --symbols A,B` inserisce una `Decision(kind="strategy", agent_id=<id dell'agente indicato>, portfolio_id=<portafoglio>, summary="Esercitazione lavoro in giornata", symbols=[...], details={"ref", "status": "approvata", "strategy", "params", "timeframe"})`; `intraday drill-revoke --portfolio --agent --ref` aggiunge la `revocata`; con `--purge` cancella invece tutte le decisioni di quel portafoglio di prova (test `test_drill_revoke_purge_deletes_decisions`). Entrambi **rifiutano** un portafoglio con righe `agent_portfolios` o non `intraday_enabled` (la decisione resta invisibile agli agenti: `activity.service._visible` mostra solo i loro portafogli e le globali).

- [ ] **Step 1: test falliti:** `test_patch_intraday_fields_admin_only` (viewer → 403), `test_patch_rejects_bad_timeframes` (`["1d"]`, `[]` → `INVALID_ORDER`), `test_patch_rejects_read_agent` (Review Focus 4), `test_patch_audited`, `test_order_day_trade_from_portal` (ordine con `day_trade=true` → DTO admin `day_trade: true`; via MCP `get_order` senza la chiave), `test_triggers_list_and_delete`, `test_pdt_endpoint_shape` (chiavi esatte, nessun `account_id`), `test_open_positions_day_qty`, `test_signals_intraday_flag`, `test_risk_profile_margin_bounds` (0 e 6 → errore, 0,5 → ok), `test_drill_strategy_refuses_agent_portfolio` (portafoglio con un agente → uscita con errore, nessuna decisione), `test_drill_strategy_is_invisible_to_agents` (dopo l'inserimento `get_decisions` via MCP di un agente qualsiasi non la restituisce; `approved_strategies` la vede sul solo portafoglio di prova).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_admin_intraday.py tests/integration/test_admin_portfolios.py tests/integration/test_admin_orders.py tests/integration/test_admin_signals.py tests/integration/test_mcp_orders.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): admin API for settings, triggers, PDT and day orders`.

---

### Task 13: Portale

**Files:**
- Create: `web/src/api/hooks/intraday.ts`
- Modify: `web/src/api/types.ts`, `web/src/pages/PortfolioDetailSettings.tsx`, `web/src/components/RiskProfileCard.tsx` (o `pages/Risk.tsx`, dove si modifica il profilo), `web/src/pages/Signals.tsx`, `web/src/pages/Positions.tsx`, `web/src/components/OrderForm.tsx`
- Test: i `.test.tsx` accanto a ciascun file

**Interfaces — Consumes:** il contratto del Task 12.
**Produces:** `useIntradayTriggers(portfolioId?: string)`, `useIntradayPdt()`, `useDeleteIntradayTriggers(portfolioId: string)`; tipi `IntradayTrigger`, `PdtLinkBudget`; `Portfolio` + i sei campi; `Order.day_trade`.

- Impostazioni del portafoglio: sezione «Lavoro in giornata» dentro `AdminOnly`: interruttore «Attivo», caselle 5m/15m/1h, scelta «Agente da svegliare» tra gli agenti con permesso trade sul portafoglio (vuoto = «nessuno: solo registrati e avviso»), «Chiusura di sicurezza (minuti prima della fine)», «Tetto operazioni di giornata su 5 sedute» (vuoto = solo la regola USA), «Sveglie massime all'ora». Testo d'aiuto: «Spento: il portafoglio lavora come oggi.»
- Profilo di rischio: «Margine del limite nella chiusura di sicurezza (%)».
- Segnali: filtro «Solo in giornata» e colonna «In giornata»; tabella «Ultimi scatti» (ora barra, portafoglio, strategia, simbolo, entrata/uscita, valore/soglia, esito in chiaro: «compito aperto», «già un compito aperto», «saltato per tetto», «nessun agente», «solo Telegram», «dopo l'orario», «non riuscito»).
- Posizioni: segno «di giornata» con la quantità quando `day_qty` > 0; riquadro «Operazioni di giornata» per collegamento: «usate X su 3 · restano Y · la prossima si libera il gg/mm», avviso se `mismatch` («il broker ne conta N»).
- Modulo ordini manuale: casella «Di giornata» (solo per gli acquisti), inviata come `day_trade`.

- [ ] **Step 1: test falliti (Vitest + msw):** `PortfolioDetailSettings` invia `PATCH` con `{intraday_enabled: true}` e la sezione non compare a un viewer; `Signals` filtra e mostra «saltato per tetto»; `Positions` mostra «di giornata 40» e «usate 2 su 3 · restano 1»; `OrderForm` invia `day_trade: true` e nasconde la casella su una vendita.
- [ ] **Step 2:** `cd web && npx vitest run src/pages/PortfolioDetailSettings.test.tsx src/pages/Signals.test.tsx src/pages/Positions.test.tsx src/components/OrderForm.test.tsx` → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `cd web && npx vitest run && npm run lint && npm run build` → PASS.
- [ ] **Step 5: commit** `feat(web): intraday settings, triggers, day positions and PDT budget`.

---

### Task 14: Storico a barre brevi

**Files:**
- Test: `tests/integration/test_intraday_history.py` (nessun codice di prodotto, salvo un difetto trovato dai test)

**Interfaces — Consumes:** `MarketDataRouter.get_bars`, `AlpacaProvider.get_bars`, `BacktestRunner`, `fetch_bars`.

- [ ] **Step 1: test** (respx sul fornitore Alpaca, nessuna rete):
  - `test_alpaca_15m_pages_are_joined`: tre pagine con `next_page_token` → tutte le barre, in ordine, `timeframe=15Min`, `feed=iex`;
  - `test_intraday_bars_cached_until_next_close`: due `get_bars(15m)` nello stesso quarto d'ora → una richiesta; dopo la chiusura della barra successiva → nuova richiesta (la chiave cambia con `end` arrotondato);
  - `test_backtest_15m_60_days_accepted`: `check_request` con 15m su 60 giorni ok, 91 giorni → `RISK_LIMIT_EXCEEDED` (limite esistente `MAX_INTRADAY_SPAN_DAYS = 90`);
  - `test_watch_fetch_has_enough_bars`: `fetch_bars(15m, 250)` sopra un fine settimana con festività restituisce almeno `min_bars` della strategia RSI 14 della prova.
- [ ] **Step 2:** eseguire; se un test fallisce per un difetto, correggere nel modulo indicato dal test e annotarlo nel report.
- [ ] **Step 3:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_history.py` → PASS.
- [ ] **Step 4: commit** `test(marketdata): intraday history paging, cache and backtest span`.

---

### Task 15: Verifica finale e messa in linea spenta

**Files:** nessuno nuovo.

- [ ] **Step 1:** suite completa: `uv run pytest -q -p no:cacheprovider` e `cd web && npx vitest run && npm run lint && npm run build` → tutto verde; `test_tool_catalog_unchanged_by_intraday` verde.
- [ ] **Step 2:** grep di guardia sul diff del ramo: nessun nome di agente o codice di portafoglio nei file nuovi (`git diff main --stat` e `grep -rn "Portfolio Manager\|Risk Manager" backend/src/smtrading/intraday` vuoto); nessuna modifica in `docs/paperclip-agents/` e in `backend/src/smtrading/mcp/`.
- [ ] **Step 3:** messa in linea **solo con gli agenti Paperclip fermi** (verifica sulla pagina Paperclip del portale: nessun ciclo in corso), backup prima, migrazione `0046` applicata, riavvio di api e worker; poi `SELECT count(*) FROM portfolios WHERE intraday_enabled` → `0`, job «intraday» presente in «Salute del sistema» e fermo a una query per giro, impronta del catalogo MCP uguale a `FROZEN_FINGERPRINT` (nessun reset delle sessioni agenti da `paperclip.sessions`).
- [ ] **Step 4:** messaggio Telegram al gruppo con l'avanzamento (barra %), in chiaro: «Lavoro in giornata costruito e in linea spento; la prova reale aspetta le chiavi del nuovo conto».

---

### Task 16: Manuale della prova reale (nessun codice)

**Files:**
- Create: `docs/readiness/2026-12-intraday-prova-alpaca.md`
- Modify: `docs/readiness/2026-12-campagna-prontezza.md` (sezione 3, una riga nella tabella)

**Prerequisito:** chiavi di un **nuovo** conto di prova Alpaca (saldo sotto 25.000 $), aperto da Luca/Matteo. **Finché le chiavi non ci sono il task resta aperto e si salta** (nessun altro task dipende da esso); appena arrivano si esegue dal passo 2.

- [ ] **Step 1: scrivere il manuale** (italiano, passi numerati, ognuno con il controllo atteso):
  1. Prerequisiti: chiavi ricevute; giorno di seduta piena, non mezza giornata; agenti tranquilli; nessuna messa in linea in corso.
  2. Collegamento broker **nuovo** `alpaca_paper` dal portale con le chiavi del nuovo conto (mai il collegamento del Portafoglio Alpaca); test in sola lettura; `GET /api/intraday/pdt` → `applies: true`.
  3. Portafoglio di prova nascosto: `in_totals=false`, **nessuna** riga `agent_portfolios`, collegato al nuovo link; `intraday_enabled=true`, `intraday_timeframes=["15m"]`, nessun agente da svegliare (gli scatti vanno su Telegram «system»).
  4. Strategia 15m (RSI o incrocio) su 1-2 titoli liquidi già seguiti dalla campagna, approvata per il solo portafoglio di prova con `python -m smtrading.cli intraday drill-strategy ...` (Task 12); controllo che il flusso prezzi della campagna non perda simboli (`python -m smtrading.marketdata.cli status` prima e dopo).
  5. Profondità dello storico gratuito: `python -m smtrading.marketdata.cli bars <SIMBOLO> 15m --days 60`; annotare la prima barra e i buchi.
  6. Per alcune sedute: scatti registrati nella pagina Segnali e messaggi Telegram alla chiusura delle barre giuste; nessun messaggio a mercato chiuso.
  7. Un'entrata manuale «di giornata» dal portale e un'uscita manuale nella stessa seduta; conteggio PDT del portale uguale a `daytrade_count` di Alpaca (riquadro Posizioni, nessun avviso di differenza).
  8. Quarta entrata dopo 3 operazioni: rifiutata con «Operazioni di giornata esaurite …»; una vendita resta sempre possibile.
  9. Rete di fine seduta: lasciare apposta una posizione di giornata aperta → ordine di sistema `day_close_minutes` prima della fine, messaggio Telegram, posizione chiusa prima della fine.
  10. Confronto dei prezzi IEX con quelli consolidati (ultimo prezzo del portale vs grafico pubblico) annotato per la pagella.
  11. Nessun disturbo alla campagna: nessun compito Paperclip nuovo, nessun cambio nei portafogli della campagna, catalogo MCP invariato.
  12. Pulizia: posizioni chiuse e conto vuoto; `DELETE /api/portfolios/{id}/intraday-triggers`; archiviazione del portafoglio di prova; collegamento disattivato; strategia della prova **cancellata** (decisioni e righe di audit del portafoglio di prova eliminate, come per le prove reali di novembre: nessuna traccia per gli agenti) con `intraday drill-revoke --purge`; esito nel documento di prontezza.
- [ ] **Step 2:** aggiungere alla tabella della sezione 3 di `2026-12-campagna-prontezza.md` la riga: `| quando arrivano le chiavi del nuovo conto di prova Alpaca (seduta piena) | Prova reale lavoro in giornata (portafoglio di prova nascosto, collegamento proprio, strategia 15m) | scatti e messaggi alle chiusure di barra, entrata e uscita di giornata, conteggio PDT uguale ad Alpaca, quarta entrata rifiutata, rete di fine seduta, nessun disturbo alla campagna, pulizia |`.
- [ ] **Step 3: commit** `docs(readiness): intraday live drill runbook`.
- [ ] **Step 4 (con le chiavi):** eseguire il manuale; esito annotato nella riga della sezione 3 («fatta: …») e nella pagella; messaggio Telegram con l'esito.
