# SMTrading — Lavoro in giornata in due modi (sveglia dell'agente e regole automatiche), costruito e spento

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** due modi di lavorare in giornata, ognuno col suo interruttore per portafoglio e **spenti ovunque**: **modo A** (`intraday_enabled`) sorveglianza dei segnali su barre brevi con sveglia dell'agente; **modo B** (`auto_rules_enabled`) regole automatiche (scalping) eseguite da SMTrading in un servizio dedicato `scalper`. Fondamenta comuni: posizioni «di giornata», rete di fine seduta, conteggio delle operazioni di giornata con il tetto facoltativo del portafoglio, lettura dello stato del conto del broker (potere d'acquisto, blocchi), storico a barre brevi, calendario, guardia MCP. La regola USA «pattern day trader» (PDT) **non esiste più dal 4/6/2026** (correzione in fondo allo spec dei due modi): nessun budget per collegamento. Prova reale su **un** nuovo conto di prova Alpaca da 30.000 $ con portafogli di prova nascosti.

**Architecture:**
- Pacchetto `intraday/` (modo A e fondamenta): calendario delle chiusure di barra (puro), posizioni di giornata e conteggio delle operazioni di giornata (dai riempimenti), sorveglianza (riusa `signals.service.strategy_signals` con un selettore), sveglia (scrittore di compiti Paperclip generalizzato al destinatario), rete di fine seduta (riusa `engine.service.close_position`), lettore dello stato del conto del broker e un unico job del worker `IntradayJob`.
- Pacchetto `autorules/` (modo B): modelli puri con parametri limitati (`breakout`, `mean_reversion`, `vwap_pullback`) e una macchina a stati `RuleRunner` **condivisa** da motore dal vivo e prova sullo storico; servizio `scalper` (stessa immagine, `python -m smtrading.autorules.main`) che legge i prezzi già pubblicati in Redis dal flusso esistente (`ticks:{symbol}` di `marketdata.cache.write_quote`) e le barre da 1 minuto aggiunte allo stesso socket Alpaca (`bars1m:{symbol}`, Redis stream), **senza** aprire connessioni proprie ai broker per i dati; ordini con il percorso esistente (`place_bracket_order`: controlli di rischio, tetto di giornata e potere d'acquisto del broker, kill switch; uscite stop/obiettivo come coppia sintetica a riposo sorvegliata da `OrderProcessor`), BrokerSync resta l'unico scrittore verso il broker e viene **svegliato** subito (`brokersync:wake`).
- Spento = come oggi: con nessun portafoglio con uno dei due interruttori il job del worker fa **una** query e torna a dormire; lo `scalper` fa una query ogni 5 s e non si abbona a nulla; il flusso prezzi ordina e serve gli stessi simboli di oggi; nessuna query in più nei controlli di rischio dei portafogli spenti.
- Gli agenti non vedono nulla: i DTO MCP sono liste esplicite di campi (`mcp/dto.py ORDER_FIELDS`, `mcp/tools_portfolio.py`), i nuovi campi e le nuove pagine stanno solo nelle API admin e nel portale.
- Ramo `feat/intraday` (worktree) da `main`; migrazioni `0046_intraday.py` (head attuale `0045` `portfolios.code`) e `0047_auto_rules.py`.

**Tech Stack:** come i piani precedenti (FastAPI, SQLAlchemy async, Alembic, Redis, httpx/respx, websockets, exchange_calendars, React + TanStack Query + Vitest). Nessuna dipendenza nuova.

**Spec:** `docs/superpowers/specs/2026-11-26-smtrading-intraday-design.md` (modo A, approvato il 26/11) e `docs/superpowers/specs/2026-11-26-smtrading-scalping-design.md` (due modi, approvato il 26/11, vincolante, con le «Scelte di Claude» 1-8). Esito del preflight applicato: `.superpowers/sdd/2026-11-26-smtrading-intraday/preflight.md` (9 conflitti, vedi «Deviazioni registrate»). Percorsi Python relativi a `backend/src/smtrading/`, test relativi a `backend/`; comandi da `backend/`: `uv run pytest -q -p no:cacheprovider <path>`.

## Global Constraints

- Valgono i vincoli dei piani 1-17: `Decimal` (mai float), `json_safe`/`to_jsonable`, `SMTError` con `ErrorCode`, audit admin con `write_audit`, nessun I/O di rete con una transazione aperta o sotto lock (R9), testi per l'utente in **italiano**, **nessun segreto** (chiavi broker o dei dati, chiave board Paperclip, ID conto) in log, errori, audit, outbox, Redis, `pdt_state` (colonna dello stato del conto, nome rimasto dalla `0046`), `auto_trades`.
- **Due interruttori, default spenti:** `portfolios.intraday_enabled` (modo A) e `portfolios.auto_rules_enabled` (modo B), bool, `server_default false`, indipendenti. `day_close_minutes` e `max_day_trades_5d` valgono per entrambi. Con gli interruttori spenti ovunque il comportamento di oggi non cambia: test di non regressione nel Task 1 (catalogo MCP) e in ogni task che tocca un percorso esistente (ordini, rischio, segnali, protezione, flusso prezzi, BrokerSync).
- **Strumenti MCP invariati:** nomi, descrizioni e schemi identici a oggi. Impronta del catalogo (`mcp.catalog.catalog_fingerprint`) sul commit `fd70037`: `fa77dcbd98f5c075a88b8cc8e1f902fb2677674ce2bbfc931a19d393593b71b0`. Nessun nuovo campo nelle risposte MCP (`ORDER_FIELDS`, `list_portfolios`, `_profile_dto` restano come sono). `day_trade` e `auto_rule_id` si impostano solo da portale/API interne/`scalper`. Nessun nuovo `ErrorCode` oltre a `DAY_TRADE_LIMIT` (già nel modo A, ora solo per il tetto proprio `max_day_trades_5d`): il rifiuto per simbolo gestito da una regola usa `RISK_LIMIT_EXCEEDED` con `details.reason = "auto_rule_owner"`; il rifiuto per importo oltre il potere d'acquisto letto dal broker usa `INSUFFICIENT_BUYING_POWER` (esistente) con `details.reason = "broker_buying_power"`.
- **Nessun cambiamento visibile agli agenti** durante la campagna: nessun compito, decisione, attività o messaggio nel flusso attività; strategie e regole di prova solo su portafogli senza righe `agent_portfolios`. I documenti in `docs/paperclip-agents/` e il pacchetto `mcp/` non si toccano.
- **Nessun nome fisso:** nessun nome o codice di agente/portafoglio/simbolo nel codice nuovo. Destinatario della sveglia = `portfolios.intraday_wake_agent_id`; nome Paperclip da `agents.name`. `RISK_MANAGER` resta solo dove è oggi (protezione).
- **Valori dello spec (modo A):** timeframe ammessi `5m`, `15m`, `1h` (default `["15m","1h"]`); `day_close_minutes` default **10**; `intraday_max_wakes_per_hour` default **6**; margine del limite di sicurezza dal profilo di rischio, default **0,5%**; operazioni di giornata contate su **5** sedute mobili (calendario `NYSE`) per il solo tetto facoltativo `max_day_trades_5d` del portafoglio (nessun tetto per collegamento, nessuna soglia di patrimonio).
- **Valori dello spec (modo B):** prezzo più vecchio di **2 s** → nessuna entrata; forbice oltre **0,10%** → nessuna entrata (entrambi parametri della regola con limiti); quota riservata nel flusso prezzi **10** simboli (`SMT_AUTO_RULES_STREAM_RESERVE`), solo con regole attive; solo acquisti (long); uscite a mercato; una regola attiva per (portafoglio, simbolo).
- **Un solo proprietario per (portafoglio, simbolo)** tra modo A, modo B e ordini manuali: con una regola attiva (o una sua operazione aperta) sul simbolo, ogni ordine non reduce-only che non viene dalla regola è rifiutato; le vendite reduce-only e le chiusure non sono **mai** rifiutate; il modo A non valuta quel simbolo su quel portafoglio e non si può attivare una regola su un simbolo di una strategia di giornata del portafoglio (Task 17).
- **Operazioni di giornata e conto del broker (correzione del 26/11 sera, Task 6R):** niente regola PDT, niente budget per collegamento, nessun confronto con il conteggio di Alpaca. Il passo 10 dei controlli di rischio vale **solo** per gli acquisti `day_trade` su portafogli in `day_scope` e rifiuta (a) a tetto proprio `max_day_trades_5d` raggiunto (contato sul solo portafoglio, `ErrorCode.DAY_TRADE_LIMIT`), (b) un'esposizione oltre la leva in giornata (`day_trade_leverage`: 1 sui collegamenti reali, al massimo 2 sugli altri; vedi il punto «Leva»), (c) un importo oltre il `buying_power` del collegamento letto dal broker, solo se la lettura è fresca (altrimenti nessun rifiuto). Le vendite **mai** (uscite a tempo e di fine seduta comprese). Lo stato del conto (`buying_power`, `multiplier`, `equity`, `trading_blocked`, `account_blocked`, `read_at`) si legge solo per i collegamenti Alpaca con un portafoglio in `day_scope`, solo GET, al meglio; avviso Telegram una volta per seduta se il conto è bloccato. Il portale **rifiuta** di accendere `auto_rules_enabled` su un collegamento **in contanti** (`multiplier` ≤ 1 o assente: regole di regolamento) o bloccato (Task 12); nessuna soglia di 25.000 $.
- **Leva (punto 6 della correzione, «Fino a 2, solo in prova»):** le entrate `day_trade` e automatiche rispettano `portfolios.leverage` (esposizione ≤ patrimonio × leva, lo stesso calcolo di `valuation.value_portfolio` già usato dal passo 9) **e** il `buying_power` letto dal broker; per i due modi la leva è al massimo **2** sui collegamenti di prova e **1** sui collegamenti reali (`LIVE_KINDS`) qualunque sia l'impostazione (`day_trade_leverage`, Task 6R). Il portale rifiuta di accendere un interruttore con leva > 2 e di alzare la leva oltre 2 su un portafoglio in `day_scope` (Task 12).
- **Rete di sicurezza:** solo a mercato aperto (`market_status(...).is_open` e `now < chiusura della seduta`); forbice oltre `MAX_SPREAD` (`marketdata.quality.is_wide`) → limite `tif=day` all'ultimo scambio meno il margine, arrotondato per difetto al tick; senza ultimo scambio con forbice larga nessun ordine e avviso; un avviso Telegram per **ogni** chiusura di sicurezza e uno se a 1 minuto dalla fine la quantità di giornata è ancora aperta. Vale per i portafogli con uno dei due interruttori.
- **Scrittore unico verso il broker:** solo BrokerSync manda ordini al broker (lock advisory invariato); lo `scalper` scrive ordini nel database con l'engine e sveglia BrokerSync con `PUBLISH brokersync:wake <order_id>` dopo il commit.
- **Telegram:** sempre `source="system", category="system"` (mai il flusso attività che gli agenti leggono).
- **Messa in linea:** solo ad **agenti Paperclip fermi** (verifica sulla pagina Paperclip del portale), backup prima, migrazioni applicate, entrambi gli interruttori `false` ovunque verificato da query, servizio `scalper` avviato e fermo (nessuna regola attiva).
- **Fuori da questo piano:** la ricerca delle fonti di prezzi a pagamento (Algo Trader Plus solo all'accensione, con domanda a Matteo); istruzioni e strumenti agli agenti e accensione (dopo il 23/12); vendite allo scoperto, opzioni, eToro, conto reale; un secondo flusso dati con le chiavi del conto di prova (vedi Deviazioni).

## Deviazioni registrate (preflight e lettura del codice)

1. (preflight 8) ~~L'avviso sulle vendite dei portafogli spenti (Task 5)~~: **tolto dal Task 6R** (la regola PDT non esiste più, correzione del 26/11 sera); nessun avviso sulle vendite.
2. (scelta 3 dello spec) «Vince la campagna»: la quota riservata di 10 posti toglie posti **solo** ai simboli seguiti soltanto dalle liste (`watchlists`, che passano al polling ogni 5 s come oggi oltre il tetto), mai a quelli di ordini, posizioni, avvisi e portale.
3. (scelta 3 dello spec) «Se il collegamento di prova ha un flusso proprio il motore usa quello»: non costruito. I dati arrivano dal fornitore configurato (una sola chiave, un solo socket per Alpaca); la quota riservata basta alla prova. Se servisse, è un lavoro a parte.
4. Le barre da 1 minuto si chiedono sullo stesso socket solo per simboli già nell'abbonamento a quotazioni e scambi; se Alpaca rifiuta per limite di simboli con le barre, il gestore riprova **senza barre** (nota nello stato), invece di dimezzare i simboli della campagna.
5. Il VWAP di seduta si calcola dal prezzo tipico `(h+l+c)/3 × volume` delle barre al minuto, uguale dal vivo e sullo storico (il campo `vw` di Alpaca non è in `Bar`).
6. Una regola ha **un** simbolo (lo spec dice «simbolo/i»): più simboli = più regole; coerente con la scelta 4 (un proprietario per simbolo).

## Review Focus

1. **Doppio invio di un ordine automatico** (stesso scatto due volte, riavvio dello `scalper` durante l'invio, barre ripassate dal riscaldamento) → un solo ordine: vincolo unico `(rule_id, trigger_key)` su `auto_trades` + `client_order_id` deterministico (idempotenza dell'engine), il riscaldamento non fa mai scattare, una regola con un'operazione aperta non entra. Test in Task 21 (`test_same_trigger_twice_one_order`, `test_restart_after_commit_no_second_order`) e Task 20 (`test_warmup_never_fires`).
2. **Entrata su prezzo vecchio, forbice larga, mercato chiuso o flusso perso** (socket in polling, worker fermo, BrokerSync fermo) → nessuna entrata, motivo contato; le uscite restano garantite. Test in Task 21 (`test_guards_block_entry`, `test_no_entry_when_symbol_polled`, `test_no_entry_when_brokersync_silent`) e Task 22 (`test_exits_survive_scalper_stop`).
3. **La modifica al flusso prezzi cambia qualcosa per gli altri simboli** (ordine, tetto, riavvii del socket, simboli della campagna spinti in polling) → senza regole attive l'abbonamento è identico a oggi; con regole la quota tocca solo i simboli delle sole liste; un rifiuto per le barre non dimezza i simboli. Test in Task 18 (`test_no_auto_rules_same_subscription`, `test_reserve_never_displaces_campaign`, `test_bar_refusal_retries_without_bars`) e Task 27 (carico).
4. **Tetto di giornata, potere d'acquisto e uscite** (tetto proprio con entrate a riposo e posizioni di giornata aperte; lettura del conto fresca, vecchia o assente; nessun residuo della regola PDT: nessun budget per collegamento, nessun avviso sulle vendite, nessun confronto con Alpaca) → a tetto raggiunto o importo oltre il `buying_power` fresco l'entrata `day_trade` è rifiutata, con lettura vecchia o assente nessun rifiuto, portafogli spenti senza query in più; le uscite automatiche (tempo, fine seduta) e ogni vendita mai. Test in Task 6R (`test_entry_refused_when_pending_fill_the_cap`, `test_link_has_no_shared_budget`, `test_buying_power_refusal`, `test_stale_or_missing_account_state_no_refusal`, `test_day_leverage_capped_at_two_on_paper`, `test_live_link_day_leverage_one`, `test_off_portfolio_never_refused`) e Task 22 (`test_time_exit_never_refused_by_day_trade_checks`).
5. **Una regola che perde non si ferma** (perdite di fila, perdita del giorno nella valuta del portafoglio, tetti di operazioni, freno di portafoglio) → stato «fermata dai freni», nessuna nuova entrata fino a riattivazione manuale, un solo avviso Telegram; le posizioni aperte restano con le loro uscite. Test in Task 23 (`test_consecutive_losses_brake`, `test_daily_loss_in_portfolio_currency`, `test_portfolio_brake_stops_all_rules`).

---

## File Structure

```
backend/src/smtrading/
  intraday/__init__.py         vuoto
  intraday/models.py           IntradayTrigger (tabella intraday_triggers), costanti degli esiti
  intraday/calendar.py         bar_closes, due_bar_close, session_window, day_close_at, last_sessions, session_after
  intraday/day_positions.py    day_quantities
  intraday/day_trades.py       FillRow, count_day_trades, DayTradeUsage, own_cap_usage, ACCOUNT_KINDS, fresh_buying_power
                               (era intraday/pdt.py: rinominato e ridotto dal Task 6R)
  intraday/account_reader.py   ACCOUNT_READ_INTERVAL_S, read_account_state, refresh_account_states
                               (era intraday/pdt_reader.py: rinominato dal Task 6R)
  intraday/watch.py            Trigger, intraday_selector, evaluate_close
  intraday/wake.py             wake_title, wake_text, Waker
  intraday/day_close.py        close_limit_price, DayCloser
  intraday/summary.py          day_summary_text
  intraday/job.py              IntradayJob
  autorules/__init__.py        vuoto
  autorules/indicators.py      MinuteSeries, SessionVwap (puri)
  autorules/models.py          Bound, ModelSpec, COMMON_PARAMS, MODELS, validate_params, Brakes, validate_brakes (puri)
  autorules/runner.py          RuleRunner, Entry, Exit (puro, condiviso con replay)
  autorules/tables.py          AutoRule, AutoRuleVersion, AutoTrade, costanti di stato/motivi
  autorules/rules.py           create_rule, update_rule, activate, pause, resume, brake, active_rules
  autorules/ownership.py       owner_refusal, owned_symbols
  autorules/quotes.py          CacheQuoteSource
  autorules/feed.py            PriceFeed (ticks pub/sub, barre bars1m, riscaldamento), stream_state
  autorules/guards.py          check_entry, motivi di blocco
  autorules/executor.py        Executor (entrata a bracket, uscite a tempo/fine seduta, annullo entrate scadute)
  autorules/tracker.py         Tracker (esecuzioni, uscite, misure)
  autorules/brakes.py          BrakeChecker
  autorules/summary.py         evening_summary_text
  autorules/replay.py          price_path, replay, ReplayResult
  autorules/service.py         Scalper
  autorules/main.py            entrata del servizio scalper
  marketdata/minute_bars.py    StreamBar, publish_minute_bar, read_minute_bars
  admin/routes_intraday.py     /api/intraday/triggers, /api/intraday/accounts, DELETE /api/portfolios/{id}/intraday-triggers
  admin/routes_auto_rules.py   /api/auto-rules/*
  (modificati) portfolios/models.py, engine/models.py, engine/service.py, risk/models.py, risk/checks.py,
  brokers/models.py, brokers/sync.py, common/errors.py, models.py, signals/service.py, paperclip/issues.py,
  positions/protection.py, positions/open_view.py, marketdata/bar_close.py, marketdata/streaming.py,
  marketdata/wscoalesce.py, marketdata/provider.py, marketdata/providers/alpaca.py, admin/schemas.py,
  admin/routes_portfolios.py, admin/routes_orders.py, admin/routes_risk.py, admin/routes_signals.py,
  api/main.py, worker/main.py, cli.py
backend/migrations/versions/0046_intraday.py, 0047_auto_rules.py
backend/tests/unit/test_mcp_catalog_frozen.py, test_intraday_calendar.py, test_intraday_day_trade_count.py,
  test_intraday_wake_text.py, test_intraday_day_close_price.py, test_paperclip_issues.py,
  test_autorules_models.py, test_autorules_runner.py, test_autorules_replay.py, test_ws_coalesce.py (+),
  test_alpaca_ws_bars.py, test_worker.py (+)
backend/tests/integration/test_intraday_schema.py, test_intraday_day_positions.py, test_intraday_day_trade_check.py,
  test_intraday_account_reader.py, test_intraday_watch.py, test_intraday_wake.py, test_intraday_day_close.py,
  test_intraday_job.py, test_admin_intraday.py, test_intraday_history.py, test_autorules_schema.py,
  test_autorules_ownership.py, test_streaming.py (+), test_brokers_sync_wake.py, test_scalper_feed.py,
  test_scalper_entry.py, test_scalper_tracker.py, test_scalper_brakes.py, test_scalper_parity.py,
  test_admin_auto_rules.py, test_autorules_load.py
docker-compose.yml (servizio scalper)
web/src/api/types.ts, web/src/api/hooks/intraday.ts, web/src/api/hooks/autoRules.ts,
  web/src/pages/PortfolioDetailSettings.tsx, web/src/components/RiskProfileCard.tsx, web/src/pages/Signals.tsx,
  web/src/pages/Positions.tsx, web/src/components/OrderForm.tsx, web/src/pages/AutoRules.tsx,
  web/src/App.tsx (rotta), navigazione admin (+ test .test.tsx accanto)
docs/readiness/2026-12-intraday-prova-alpaca.md, docs/readiness/2026-12-campagna-prontezza.md
```

## Ordine e proprietà dei file

| Ondata | Task | Note |
|---|---|---|
| A | 1 → 2 | guardia MCP prima di tutto; poi schema comune (prerequisito di tutto) |
| B | 3 ∥ 7 ∥ 14 ∥ 15 | 3: `intraday/calendar.py`, `marketdata/bar_close.py` (alias); 7: `paperclip/issues.py`, `positions/protection.py`; 14: solo test di storico; 15: `autorules/{indicators,models,runner}.py` (puri) |
| C | 4 → 5 → 6 → 6R | `intraday/{day_positions,pdt,pdt_reader}.py`, `risk/checks.py` (passo 10), `engine/service.py` (day_trade); 6R rinomina in `intraday/{day_trades,account_reader}.py` e rifà il passo 10 (correzione PDT) |
| D | 8 → 9 | `signals/service.py`, `intraday/{watch,wake}.py` |
| E | 10 | `intraday/day_close.py`, `engine/service.py` (`close_position` con limite) — dopo 5 (stesso file) |
| F | 11 | `intraday/{job,summary}.py`, `worker/main.py` |
| G | 12 → 13 | API admin del modo A e interruttore del modo B, poi `web/` |
| H | 16 → (17 ∥ 18 ∥ 19) | 16: migrazione `0047`, `autorules/{tables,rules}.py`, `SOURCES` in `marketdata/streaming.py`, `admin/routes_portfolios.py`; 17: `risk/checks.py` (passo 11), `engine/service.py`, `worker/main.py` (dopo 11); 18: `marketdata/{streaming,wscoalesce,provider,minute_bars}.py`, `providers/alpaca.py` (dopo 16, stesso file); 19: `brokers/sync.py` |
| I | 20 → 21 → 22 → 23 | `autorules/{quotes,feed,service,main,guards,executor,tracker,brakes,summary}.py`, `docker-compose.yml` |
| J | 24 | `autorules/replay.py`, parità motore ↔ storico |
| K | 25 → 26 | `admin/routes_auto_rules.py`, poi `web/` pagina «Regole automatiche» |
| L | 27 | prova di carico (solo test) |
| M | 28 → 29 | verifica finale e messa in linea spenta; manuale della prova reale dei due modi |

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
  - In `test_mcp_portfolio.py`, `test_list_portfolios_keys_frozen`: l'insieme delle chiavi di ogni voce è esattamente `{"id","name","code","base_currency","status","in_totals","risk_profile","permission"} | chiavi di _mode_field` (copiare l'insieme osservato oggi) e non contiene nessuna chiave che inizia con `intraday`, `day_`, `auto_`.
  - In `test_mcp_orders.py`, `test_order_dto_has_no_day_trade`: l'ordine restituito da `place_order` via MCP non ha le chiavi `day_trade` né `auto_rule_id`; le chiavi sono esattamente `ORDER_FIELDS`.
- [ ] **Step 3: eseguire** `uv run pytest -q -p no:cacheprovider tests/unit/test_mcp_catalog_frozen.py tests/integration/test_mcp_portfolio.py tests/integration/test_mcp_orders.py` → PASS (sulla base passano già: sono guardie).
- [ ] **Step 4: commit** `test(mcp): freeze the tool catalog and DTO keys for the intraday work`.

---

### Task 2: Schema comune, codice d'errore, `day_trade` sugli ordini

**Files:**
- Create: `migrations/versions/0046_intraday.py`, `intraday/__init__.py`, `intraday/models.py`
- Modify: `portfolios/models.py`, `engine/models.py`, `risk/models.py`, `brokers/models.py`, `common/errors.py`, `models.py` (registro), `engine/service.py` (`PlaceOrderRequest.day_trade`, `_persist`, `_same_params`)
- Test: `tests/integration/test_migrations.py` (drift esistente), `tests/integration/test_intraday_schema.py`

**Interfaces — Produces:**
- `Portfolio`: `intraday_enabled: bool` (`server_default false`); **`auto_rules_enabled: bool` (`server_default false`)**; `intraday_timeframes: list[str]` JSONB not null `server_default '["15m","1h"]'`; `intraday_wake_agent_id: UUID|None` FK `agents.id` `ON DELETE SET NULL`; `day_close_minutes: int` not null `server_default 10`, CHECK `2 <= x <= 60`; `max_day_trades_5d: int|None`, CHECK `x >= 0`; `intraday_max_wakes_per_hour: int` not null `server_default 6`, CHECK `1 <= x <= 60`.
- `Order.day_trade: bool` default false, `server_default false`. `PlaceOrderRequest.day_trade: bool = False` copiato su `Order` in `_persist`; `_same_params` confronta anche `day_trade`.
- `RiskProfile.day_close_limit_margin_pct: Decimal` not null `server_default 0.5`, CHECK `0 < x <= 5`.
- `BrokerLink.pdt_state: dict|None` JSONB; `BrokerLink.pdt_state_at: datetime|None`.
- `IntradayTrigger` (`intraday_triggers`): `id BIGINT identity; created_at timestamptz server_default now(); portfolio_id UUID FK portfolios ON DELETE CASCADE; strategy_ref String(128); strategy_name String(200); symbol String(32); rule String(8)` CHECK `entry|exit`; `timeframe String(8); bar_close timestamptz; value NUM|None; threshold NUM|None; price NUM|None; rule_text String(200); outcome String(16)` CHECK su `pending, woken, telegram, capped, issue_open, no_agent, late, failed`; `issue_ident String(64)|None; error String(200)|None`. Indici `(portfolio_id, created_at)`, `(portfolio_id, symbol, id)`. Righe mai modificate se non `outcome`/`issue_ident`/`error` da `pending` all'esito finale; cancellate solo con il portafoglio o dall'endpoint di pulizia (Task 12).
- Costanti in `intraday/models.py`: `INTRADAY_TIMEFRAMES = ("5m", "15m", "1h")`, `DEFAULT_TIMEFRAMES = ("15m", "1h")`, gli 8 esiti come costanti stringa (`PENDING, WOKEN, TELEGRAM, CAPPED, ISSUE_OPEN, NO_AGENT, LATE, FAILED`).
- `ErrorCode.DAY_TRADE_LIMIT = "DAY_TRADE_LIMIT"`.
- `def day_scope(p: Portfolio) -> bool` in `intraday/models.py`: `p.intraday_enabled or p.auto_rules_enabled` (usato dal passo 10 dei controlli, rete di fine seduta, lettore dello stato del conto, job) e la condizione SQL gemella `DAY_SCOPE = or_(Portfolio.intraday_enabled, Portfolio.auto_rules_enabled)`.

- [ ] **Step 1: test falliti:**
  - `test_migration_matches_models` (drift, esistente) rosso finché modelli e migrazione non coincidono;
  - `test_intraday_defaults_are_off`: un portafoglio creato con `create_portfolio` ha `intraday_enabled is False`, `auto_rules_enabled is False`, `intraday_timeframes == ["15m","1h"]`, `day_close_minutes == 10`, `intraday_max_wakes_per_hour == 6`, `intraday_wake_agent_id is None`, `max_day_trades_5d is None`; un ordine piazzato senza `day_trade` ha `day_trade is False`; il profilo «Default» ha `day_close_limit_margin_pct == Decimal("0.5")`;
  - `test_day_scope`: `(False, False) → False`, `(True, False) → True`, `(False, True) → True`; la query `select(Portfolio).where(DAY_SCOPE)` dà gli stessi portafogli;
  - `test_day_trade_propagates_to_order`: `place_order(..., PlaceOrderRequest(..., day_trade=True))` → `order.day_trade is True`;
  - `test_client_order_id_reused_with_other_day_trade_is_invalid`: stesso `client_order_id`, `day_trade` diverso → `INVALID_ORDER`;
  - `test_intraday_checks`: `day_close_minutes=1` e `intraday_max_wakes_per_hour=0` violano i CHECK (IntegrityError); `rule='both'` su `intraday_triggers` viola il CHECK;
  - `test_downgrade_0046_and_back`: `alembic downgrade 0045` poi `upgrade head` senza errori.
- [ ] **Step 2:** eseguire i test → FAIL.
- [ ] **Step 3:** modelli, migrazione `0046_intraday.py` (`down_revision = "0045"`, il nome esatto della revisione 0045 si legge dal file), `PlaceOrderRequest.day_trade`, `ErrorCode`, `day_scope`.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_migrations.py tests/integration/test_intraday_schema.py tests/integration/test_order_service.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): shared schema behind intraday_enabled and auto_rules_enabled (off by default)`.

---

### Task 3: Calendario di giornata (puro)

**Files:**
- Create: `intraday/calendar.py`
- Modify: `marketdata/bar_close.py` (una riga: alias pubblico)
- Test: `tests/unit/test_intraday_calendar.py`

**Interfaces — Consumes:** `instruments.market_hours.session_on, local_date, CALENDARS, _calendar` (fuso dell'exchange: `_calendar(CALENDARS[m]).tz`); `marketdata.bar_close._DURATION` tramite l'alias pubblico (nessuna nuova mappa dei timeframe).
**Produces:**
- In `marketdata/bar_close.py`: `TIMEFRAME_DURATION = _DURATION` (alias pubblico, nessun altro cambiamento).
- `BAR_DELAY = timedelta(seconds=60)` (la barra è disponibile dal fornitore).
- `def session_window(market: str, now: datetime) -> tuple[datetime, datetime] | None` — (apertura, chiusura) UTC della seduta della data locale di `now`; None senza seduta o per mercati senza calendario (CRYPTO, FX: niente giornata).
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
tz = exchange tz; step = TIMEFRAME_DURATION[tf]
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
  - `test_crypto_has_no_window`: `session_window("CRYPTO", ...) is None`, `bar_closes("CRYPTO", m15, ...) == []`;
  - `test_duration_alias_is_the_same_map`: `TIMEFRAME_DURATION is bar_close._DURATION`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare le sei funzioni in `intraday/calendar.py` e l'alias.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_calendar.py tests/unit/test_bar_close.py` (se il file esiste; altrimenti i test di `bar_close` esistenti) → PASS.
- [ ] **Step 5: commit** `feat(intraday): bar-close and session calendar`.

---

### Task 4: Posizioni di giornata e conteggio PDT

> **Superato in parte dal Task 6R** (correzione del 26/11 sera: la regola PDT non esiste più). Restano come scritti qui solo `day_quantities`, `FillRow`, `count_day_trades` e i loro test; il resto si rifà nel Task 6R.

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
- `async def link_budget(session, link_id: UUID, now: datetime) -> PdtBudget` — riempimenti di **tutti** gli ordini con `orders.broker_link_id == link_id` su strumenti `us_equity` nelle `last_sessions(PDT_MARKET, now, 5)` (quindi **tutti i portafogli e i due modi**); `limit = PDT_MAX` se applica, altrimenti None; `pending` = simboli con quantità di giornata aperta su portafogli del collegamento + simboli con un acquisto `day_trade` a riposo (`FILLABLE_STATUSES`, non reduce-only) e nessuna posizione di giornata; `remaining = limit - used - pending` (mai sotto 0) o None; `frees_on` = `session_after(d, 5)` della seduta più vecchia della finestra con operazioni; `broker_count` = `pdt_state["daytrade_count"]`, `mismatch = broker_count is not None and broker_count != used`.
- `async def entry_budget(session, portfolio: Portfolio, symbol: str, now: datetime) -> PdtBudget` — combina il budget del collegamento (se `broker_link_id` e tipo in `PDT_KINDS`) con il tetto proprio `max_day_trades_5d` (contato sui riempimenti **del solo portafoglio**): `remaining = min` dei due; un `symbol` che ha già una posizione di giornata o un acquisto `day_trade` a riposo non aggiunge un'operazione futura (lo si toglie da `pending`). Senza collegamento PDT e senza tetto proprio: `remaining=None`.

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
  - unit: `test_pdt_applies`: `("t212_demo", None) → False`; `("alpaca_paper", None) → True`; `multiplier "2", equity "24999.99" → True`; `multiplier "1", equity "1000" → False` (conto contante); `multiplier "4", equity "30000", last_equity "24000" → True`; `multiplier "4", equity "30000", last_equity "30000" → False`;
  - integration: `test_day_quantities_mixed_position` (60 comprati ieri, 40 oggi con `day_trade`, nessuna vendita → `{"AAPL": 40}`; vendita oggi di 50 → `{}`); `test_day_quantities_ignores_non_day_trade_buys`;
  - integration: `test_link_budget_counts_all_portfolios_of_the_link` (2 portafogli sullo stesso collegamento, uno con `intraday_enabled`, l'altro con `auto_rules_enabled`: 1 operazione ciascuno → `used == 2`, `remaining == 1`), `test_link_budget_window_with_holiday` (operazioni il 23/11 e il 30/11: al 30/11 `used == 2`; al 1/12 il 23/11 esce → `used == 1`; `frees_on == date(2026,12,1)` al 30/11), `test_pending_counts_open_day_positions_and_resting_entries`, `test_mismatch_with_broker_count` (`pdt_state={"daytrade_count": 2}`, nostro 1 → `mismatch is True`);
  - integration: `test_entry_budget_own_cap` (`max_day_trades_5d=1`, senza collegamento, 1 operazione → `remaining == 0`), `test_own_cap_ignores_other_portfolios_of_link` (portafoglio con `max_day_trades_5d=1`, l'altro portafoglio dello stesso collegamento ha 1 operazione, conto ≥ 25.000 $ → `remaining == 1`), `test_entry_budget_same_symbol_adds_nothing`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare `day_positions.py` e `pdt.py` (query: `Fill` ⋈ `Order` ⋈ `Instrument`, filtro su `Fill.created_at >= apertura della seduta più vecchia della finestra`).
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_pdt_count.py tests/integration/test_intraday_day_positions.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): day positions and the 5-session day-trade count`.

---

### Task 5: Controllo PDT prima dell'invio e avviso sulle vendite

> **Superato in parte dal Task 6R** (correzione del 26/11 sera: la regola PDT non esiste più). Restano come scritti qui solo `pre_trade_check(..., day_trade)`, il passaggio di `day_trade` da `_run_check` e la posizione del passo 10; il resto si rifà nel Task 6R.

**Files:**
- Modify: `risk/checks.py` (`pre_trade_check`, `_run`), `engine/service.py` (`_run_check` passa `day_trade`; `_place` chiama l'avviso), `intraday/pdt.py` (`warn_day_trade_sell`)
- Test: `tests/integration/test_intraday_pdt_check.py`

**Interfaces — Consumes:** `entry_budget`, `link_budget`, `count_day_trades` (Task 4), `day_scope` (Task 2).
**Produces:**
- `pre_trade_check(..., day_trade: bool = False)`; in `_run` un nuovo **passo 10** dopo il passo 9 esistente (potere d'acquisto, `# 9:` in `risk/checks.py`), con commento `# 10: day-trade budget (PDT)`: se `side == buy and day_trade and day_scope(portfolio)` e `entry_budget(...).remaining` non è None e `<= 0` → `_Reject(ErrorCode.DAY_TRADE_LIMIT, msg, {"used", "limit", "remaining": 0, "frees_on"})`. Testo: `"Operazioni di giornata esaurite: {used} usate su {limit} nelle ultime 5 sedute (più {pending} già aperte), ne restano 0; la prossima si libera il {gg/mm}"` (senza `frees_on`: «quando una delle aperte si chiude senza operazione»). Portafoglio fuori da `day_scope`: nessuna query.
- `async def warn_day_trade_sell(session, order: Order, now: datetime) -> None` — per un ordine di vendita non rifiutato di un portafoglio **fuori** da `day_scope`, collegato a un link in `PDT_KINDS` che ha almeno un portafoglio non archiviato in `day_scope`: se la vendita chiuderebbe quantità comprata nella seduta e `used + 1 >= PDT_MAX` con `pdt_applies` → un `enqueue(source="system", category="system", level="warning")` «Attenzione regola USA sui conti piccoli: la vendita di {symbol} su {portafoglio} sarebbe l'operazione di giornata n. {used+1} su {PDT_MAX} nelle ultime 5 sedute del collegamento {link}». Esce subito (nessuna query oltre la prima) se il portafoglio non ha `broker_link_id`. Ambito più stretto dello spec del modo A: **Deviazione 1**.
- Chiamata in `engine/service._place` dopo `_activate`, solo con `order.side == sell` e stato non `rejected`.

- [ ] **Step 1: test falliti:**
  - `test_fourth_day_trade_entry_refused`: portafoglio `intraday_enabled`, collegamento `alpaca_paper` con `pdt_state` sotto 25.000 $, 3 operazioni nelle 5 sedute → acquisto `day_trade=True` rifiutato `DAY_TRADE_LIMIT`, `details["remaining"] == 0`, testo contiene «ne restano 0» e la data di `frees_on`;
  - `test_auto_rules_portfolio_entry_refused`: stesso scenario con `auto_rules_enabled` al posto di `intraday_enabled` → rifiutato;
  - `test_budget_sums_both_modes` (Review Focus 4): portafoglio A (`intraday_enabled`) 1 operazione, portafoglio B (`auto_rules_enabled`) 1 operazione, stesso collegamento sotto 25.000 $, una posizione di giornata aperta su B → acquisto `day_trade` su A di un terzo simbolo rifiutato; lo stesso su B rifiutato;
  - `test_entry_refused_when_pending_fill_the_budget` (Review Focus 4): 2 usate, una posizione di giornata aperta su MSFT → acquisto `day_trade` su AAPL rifiutato; un secondo acquisto `day_trade` su MSFT accettato;
  - `test_sells_never_refused`: stesso scenario, vendita reduce-only della posizione → accettata;
  - `test_non_day_trade_buy_not_checked`: `day_trade=False` → accettato;
  - `test_off_portfolio_never_refused` (non regressione): stesso collegamento, portafoglio con entrambi gli interruttori spenti, acquisto `day_trade=True` → accettato, nessuna query PDT (contatore di query del motore SQLAlchemy uguale a prima della modifica);
  - `test_cash_account_not_limited`: `pdt_state={"multiplier": "1", ...}` → accettato;
  - `test_off_portfolio_sell_warns_on_shared_link`: portafoglio spento sullo stesso collegamento di uno di giornata, 2 operazioni, acquisto e vendita oggi → una riga `telegram_outbox` `category="system"` col testo «operazione di giornata n. 3 su 3»;
  - `test_no_warning_without_day_scope_on_link` (non regressione): stesso scenario senza portafogli in `day_scope` sul collegamento → nessuna riga outbox;
  - `test_preview_reports_day_trade_limit`: `preview_order` con il budget finito → `ok=False`, `code == "DAY_TRADE_LIMIT"`, nessuna riga outbox.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_pdt_check.py tests/integration/test_risk_checks.py tests/integration/test_order_service.py tests/integration/test_admin_orders.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): refuse day-trade entries when the PDT budget is spent`.

---

### Task 6: Lettura PDT dal broker e confronto

> **Superato in parte dal Task 6R** (correzione del 26/11 sera: la regola PDT non esiste più). Restano come scritti qui solo la lettura in sola GET senza transazione aperta, la scelta dei collegamenti (Alpaca con un portafoglio in `DAY_SCOPE`) e la gestione degli errori; il resto si rifà nel Task 6R.

**Files:**
- Create: `intraday/pdt_reader.py`
- Test: `tests/integration/test_intraday_pdt_reader.py`

**Interfaces — Consumes:** `brokers.service.open_adapter`, `BrokerAdapter.get_account() -> AccountSnapshot` (`raw` è il JSON di `GET /v2/account`), `link_budget`, `DAY_SCOPE`, `jobs.runs.run_job_once`, `telegram.outbox.enqueue`.
**Produces:**
- `PDT_READ_INTERVAL_S = 300`.
- `async def read_pdt_state(sessionmaker, http, link_id: UUID) -> dict` — una lettura dell'account senza transazione aperta, salva e restituisce `pdt_state = {"equity", "last_equity", "multiplier", "daytrade_count", "pattern_day_trader"}` (stringhe/int/bool da `raw`, nient'altro: niente ID conto) e `pdt_state_at`; errori di lettura → `SMTError(PROVIDER_UNAVAILABLE)`. Usata anche dal PATCH di `auto_rules_enabled` (Task 12).
- `async def refresh_pdt_states(sessionmaker, http, now: datetime) -> int` — numero di collegamenti letti. Collegamenti `enabled` con `kind in PDT_KINDS` e almeno un portafoglio non archiviato in `DAY_SCOPE`; `read_pdt_state` per ognuno; se `link_budget(...).used != daytrade_count` → `log.warning` (senza ID conto) e, una volta per `run_job_once("intraday_pdt_mismatch", f"{link_id}:{data}:{nostro}:{broker}")`, Telegram «Conteggio operazioni di giornata diverso dal broker su {nome collegamento}: SMTrading {nostro}, Alpaca {broker}». Con `pattern_day_trader == True` un avviso «conto segnato come day trader dal broker» una volta per seduta. Un errore di lettura si registra nel log e il collegamento si riprova al giro dopo.

- [ ] **Step 1: test falliti** (respx sul `GET /v2/account` del fake Alpaca paper, `assert_all_mocked=True`):
  - `test_reads_only_links_with_day_scope_portfolios`: tre collegamenti, uno con un portafoglio `intraday_enabled`, uno con un portafoglio `auto_rules_enabled`, uno senza → due richieste, `pdt_state` salvato solo lì, con le 5 chiavi esatte;
  - `test_mismatch_alert_once`: broker 2, nostro 1 → una riga outbox; secondo giro uguale → nessuna nuova riga;
  - `test_read_failure_keeps_old_state`: HTTP 500 → `pdt_state` invariato, nessuna eccezione da `refresh_pdt_states`; `read_pdt_state` solleva `PROVIDER_UNAVAILABLE`;
  - `test_no_secret_in_state_or_logs`: `caplog` e `pdt_state` non contengono la key ID canarino né l'ID conto.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_pdt_reader.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): read the broker's day-trade count and compare it`.

---

### Task 6R: adeguamento alla fine della regola PDT

> Correzione vincolante: sezione «Correzione del 26/11/2026 sera — la regola PDT non esiste più» in fondo a `docs/superpowers/specs/2026-11-26-smtrading-scalping-design.md`. Prevale sulle parti PDT di entrambi gli spec e dei Task 4-6. Comprende il punto 6 (leva, Matteo 26/11: «Fino a 2, solo in prova»). Si lavora sul codice già sul ramo (commit `ecb7faa`, `26959f3`, `a20ac38`, `d1795f6`, `37cdcf3`); nessuna migrazione nuova: le colonne `broker_links.pdt_state`/`pdt_state_at` restano (nome della `0046`) e da qui contengono lo **stato del conto**.

**Files:**
- Rename (`git mv`) e modifica: `intraday/pdt.py` → `intraday/day_trades.py`; `intraday/pdt_reader.py` → `intraday/account_reader.py`
- Modify: `risk/checks.py` (import, `_check_day_trade_budget` → due funzioni, passo 10), `engine/service.py` (togliere l'avviso sulle vendite), `common/errors.py` (solo commento di `DAY_TRADE_LIMIT`), `brokers/models.py` (solo commento su `pdt_state`/`pdt_state_at`), `engine/models.py` (commento di `day_trade`: «conteggio delle operazioni di giornata», non «PDT count»), `intraday/models.py` (docstring di `day_scope`: «tetto di giornata, rete di fine seduta, lettura del conto, job»)
- Rename (`git mv`) e riscrittura dei test: `tests/unit/test_intraday_pdt_count.py` → `tests/unit/test_intraday_day_trade_count.py`; `tests/integration/test_intraday_pdt_check.py` → `tests/integration/test_intraday_day_trade_check.py`; `tests/integration/test_intraday_pdt_reader.py` → `tests/integration/test_intraday_account_reader.py`; modifica di `tests/integration/test_intraday_day_positions.py`

**Interfaces — Consumes:** `count_day_trades`, `day_quantities` (Task 4, invariati), `day_scope`/`DAY_SCOPE` (Task 2), `open_adapter`, `ReadOnlyHttp`, `run_job_once`, `enqueue`, `QuoteSource.get_fx_rate`.

**Da cancellare (nessun alias, nessun resto):**
- in `intraday/pdt.py`: `PDT_MAX`, `PDT_EQUITY_MIN`, `pdt_applies`, `_dec` (se non più usato), `PdtBudget` (sostituito, vedi sotto), `_link_budget`, `link_budget`, `_release`, `entry_budget` (sostituito), `_would_close_day_quantity`, `_warn`, `warn_day_trade_sell`, il logger e gli import di `BrokerLink`, `OrderStatus`, `DAY_SCOPE`, `day_scope`, `PortfolioStatus`, `session_window`, `enqueue` rimasti senza uso; il parametro `link_id` di `_day_trades` (resta solo `portfolio_id`);
- in `engine/service.py`: l'import `from smtrading.intraday.pdt import warn_day_trade_sell` e il blocco `if order.side == OrderSide.sell and order.status != OrderStatus.rejected: await warn_day_trade_sell(...)` in `_place`;
- in `intraday/pdt_reader.py`: `link_budget`/`PDT_MARKET` dagli import, `_compare` (confronto con `daytrade_count` e avviso «conto segnato come day trader»), i nomi di job `intraday_pdt_mismatch` e `intraday_pdt_flag`, i campi `last_equity`, `daytrade_count`, `pattern_day_trader` da `_state_from`;
- in `risk/checks.py`: `_check_day_trade_budget` e il testo «Operazioni di giornata esaurite … ne restano 0».

**Da rinominare:**
- `PDT_KINDS` → `ACCOUNT_KINDS` (stesso valore `{"alpaca_paper", "alpaca_live"}`: i collegamenti di cui si legge il conto), in `intraday/day_trades.py`;
- `PDT_MARKET` → `DAY_TRADE_MARKET` (`"NYSE"`), `PDT_SESSIONS` → `DAY_TRADE_SESSIONS` (`5`);
- `PDT_READ_INTERVAL_S` → `ACCOUNT_READ_INTERVAL_S` (`300`), `read_pdt_state` → `read_account_state`, `refresh_pdt_states` → `refresh_account_states`, logger `smtrading.intraday.account_reader`.

**Produces:**
- `intraday/day_trades.py` (docstring nuova: «Conteggio delle operazioni di giornata dai nostri riempimenti, per il tetto facoltativo del portafoglio e per le misure; la regola PDT non esiste più dal 4/6/2026»):
  - `FillRow`, `count_day_trades` **invariati** (stesso algoritmo e stessi test);
  - `@dataclass(frozen=True) class DayTradeUsage: limit: int | None; used: int; pending: int; remaining: int | None; frees_on: date | None` (al posto di `PdtBudget`: niente `applies`, `broker_count`, `mismatch`);
  - `async def own_cap_usage(session, portfolio: Portfolio, now: datetime, symbol: str | None = None) -> DayTradeUsage` (al posto di `entry_budget`): `used` = operazioni del **solo** portafoglio nella finestra (`_day_trades(..., portfolio_id=...)`), `pending` = coppie di `_pending_pairs`/`_us_pairs` del solo portafoglio meno 1 se `(portfolio.id, symbol)` è tra esse, `limit = portfolio.max_day_trades_5d`, `remaining = max(0, limit - used - pending)` o None senza tetto, `frees_on = _frees_on(...)`. Con `symbol=None` (portale) nessuna coppia si toglie;
  - `ACCOUNT_STATE_MAX_AGE = timedelta(seconds=2 * 300)` (due giri di lettura; il valore 300 si importa come costante condivisa, non duplicato: `ACCOUNT_READ_INTERVAL_S` vive qui e `account_reader` lo importa, così `risk/checks.py` non importa `account_reader` → nessun import circolare con `brokers.service`);
  - `DAY_TRADE_MAX_LEVERAGE = Decimal(2)`; `def day_trade_leverage(portfolio: Portfolio, link: BrokerLink | None) -> Decimal` (pura): `Decimal(1)` se `link` è di un tipo in `brokers.live.LIVE_KINDS` (conto reale: leva 1 qualunque sia l'impostazione, finché Matteo non decide), altrimenti `min(portfolio.leverage, DAY_TRADE_MAX_LEVERAGE)`;
  - `def fresh_buying_power(link: BrokerLink | None, now: datetime) -> Decimal | None` (pura): None se `link` è None, `link.kind not in ACCOUNT_KINDS`, `link.pdt_state` None, `pdt_state_at` None o più vecchio di `ACCOUNT_STATE_MAX_AGE`, `buying_power` assente o non numerico (stati vecchi con chiavi PDT inclusi); altrimenti `Decimal(buying_power)`.
- Passo 10 in `risk/checks.py`, commento `# 10: day trades: the portfolio's own cap and the broker's buying power. Only buys flagged day_trade of a portfolio in day_scope; sells are never refused`:
  ```python
  if side == OrderSide.buy and day_trade and day_scope(portfolio):
      await _check_own_day_trade_cap(session, portfolio, instrument, now)   # solo se max_day_trades_5d non è None: nessuna query altrimenti
      link = await session.get(BrokerLink, portfolio.broker_link_id) if portfolio.broker_link_id else None
      _check_day_trade_leverage(portfolio, link, valuation, notional_eur, costs)   # se l'esposizione cresce (`increases`)
      await _check_broker_buying_power(qs, link, ccy, order_notional, now)
  ```
  - `_check_own_day_trade_cap`: `remaining <= 0` → `_Reject(ErrorCode.DAY_TRADE_LIMIT, "Tetto operazioni di giornata del portafoglio raggiunto: {used} usate su {limit} nelle ultime 5 sedute (più {pending} già aperte); la prossima si libera il {gg/mm}", {"used", "limit", "remaining": 0, "frees_on"})` (senza `frees_on`: «quando una delle aperte si chiude senza operazione»);
  - `_check_day_trade_leverage` (punto 6 dello spec, **riusa** il calcolo del passo 9 di `valuation.value_portfolio`: `buying_power = equity × leverage − exposure`, nessuna formula nuova): `lev = day_trade_leverage(portfolio, link)`; se `lev < portfolio.leverage` (altrimenti il passo 9 ha già controllato la stessa cosa) e `valuation.equity_raw * lev - valuation.exposure_raw - costs - notional_eur < 0` → `_Reject(ErrorCode.INSUFFICIENT_BUYING_POWER, "Esposizione oltre la leva ammessa per le operazioni di giornata ({lev}) su questo portafoglio", {"reason": "day_trade_leverage", "leverage": str(lev)})`. Le variabili `valuation`, `costs`, `notional_eur`, `increases` sono quelle del passo 9: verificare che siano definite su tutti i percorsi che arrivano al passo 10 (oggi `costs` nasce dentro il ramo del calcolo di `bp_after`);
  - `_check_broker_buying_power`: solo con un collegamento; `bp = fresh_buying_power(link, now)`; None → nessun rifiuto; importo stimato dell'ordine (`order_notional` nella valuta dello strumento, convertito in USD con `qs.get_fx_rate(ccy, "USD")` se `ccy != "USD"`) `> bp` → `_Reject(ErrorCode.INSUFFICIENT_BUYING_POWER, "Importo di {x} $ oltre il potere d'acquisto del conto {link} ({bp} $, letto alle {hh:mm})", {"reason": "broker_buying_power", "notional": str, "buying_power": str, "read_at": iso})`. Codice esistente: nessun nuovo `ErrorCode`, impronta MCP invariata.
  - Ordine dei passi invariato (dopo il 9, prima dell'11 del Task 17); un portafoglio fuori da `day_scope` non fa nessuna query in più.
- `intraday/account_reader.py` (docstring: «Stato del conto del broker, letto solo con GET e solo per i collegamenti con un portafoglio in `DAY_SCOPE`; al posto del confronto PDT»):
  - `async def read_account_state(sessionmaker, http, link_id) -> dict` — come `read_pdt_state` (stessa lettura senza transazione aperta, `ReadOnlyHttp`, stessi errori `PROVIDER_UNAVAILABLE` senza testo dell'eccezione), salva in `pdt_state` **esattamente** `{"buying_power": str|None, "multiplier": str|None, "equity": str|None, "trading_blocked": bool|None, "account_blocked": bool|None, "read_at": iso}` e `pdt_state_at = read_at`;
  - `async def refresh_account_states(sessionmaker, http, now) -> int` — stessi collegamenti di prima (`enabled`, `kind in ACCOUNT_KINDS`, un portafoglio non archiviato in `DAY_SCOPE`), stesso comportamento su errore (log senza segreti, giro dopo); al posto di `_compare`, `_alert_blocked`: se `trading_blocked` o `account_blocked` è `True`, una volta per `run_job_once("intraday_account_blocked", f"{link_id}:{data NYSE}")` → Telegram `source="system", category="system", level="warning"` «Conto {nome collegamento} bloccato dal broker ({operatività/conto}): nessun nuovo ordine passerà finché il broker non lo sblocca».

- [ ] **Step 1: `git mv` dei due moduli e dei tre file di test, poi test falliti:**
  - `tests/unit/test_intraday_day_trade_count.py`: **tenere** `test_buy_buy_sell_is_one`, `test_buy_sell_buy_sell_is_two`, `test_buy_sell_sell_is_two`, `test_two_fills_of_one_sell_order_count_once`, `test_overnight_shares_sold_today_is_not_a_day_trade`, `test_days_are_separate`, `test_symbols_are_separate`, `test_equal_timestamps_buys_sort_first` (solo l'import cambia); **cancellare** `test_pdt_applies` (con i suoi parametri); **nuovo** `test_fresh_buying_power`: link `alpaca_paper` con `buying_power "1000"` letto 9 minuti fa → `Decimal("1000")`; 11 minuti fa → None; `pdt_state=None` → None; `{"daytrade_count": 1}` (stato vecchio) → None; `buying_power "abc"` → None; `kind="t212_demo"` → None.
  - `tests/integration/test_intraday_day_positions.py`: tenere i tre `test_day_quantities_*`; **cancellare** `test_link_budget_counts_all_portfolios_of_the_link` e `test_mismatch_with_broker_count`; **riscrivere** su `own_cap_usage` di un solo portafoglio: `test_link_budget_window_with_holiday` → `test_own_usage_window_with_holiday` (stesse date e stesse asserzioni su `used` e `frees_on`, `max_day_trades_5d=3`), `test_pending_counts_open_day_positions_and_resting_entries` → `test_own_usage_pending_counts_open_day_positions_and_resting_entries` (`pending == 2`), `test_entry_budget_own_cap` → `test_own_cap_usage_reaches_zero` (`remaining == 0`), `test_entry_budget_without_any_limit` → `test_own_usage_without_cap` (`limit is None`, `remaining is None`, `used == 1`: il conteggio c'è anche senza tetto), `test_own_cap_ignores_other_portfolios_of_link` (resta, senza la frase «conto ≥ 25.000 $» e senza `pdt_state`: `remaining == 1`), `test_entry_budget_same_symbol_adds_nothing` → `test_own_usage_same_symbol_adds_nothing` (stesse asserzioni 1 e 0).
  - `tests/integration/test_intraday_day_trade_check.py`: costante `UNDER` sostituita da `account(bp="100000", minutes_ago=1)` che costruisce lo stato a 6 chiavi; **cancellare** `test_fourth_day_trade_entry_refused`, `test_budget_sums_both_modes`, `test_cash_account_not_limited`, `test_off_portfolio_sell_warns_on_shared_link`, `test_no_warning_without_day_scope_on_link`, `test_no_warning_when_sell_closes_overnight_shares`, `test_no_warning_without_link`, `test_place_order_sell_warns`, `test_warning_failure_never_fails_the_placement`, `test_rejected_sell_does_not_warn`; **nuovi/riscritti**:
    - `test_own_cap_entry_refused`: `intraday_enabled`, `max_day_trades_5d=1`, 1 operazione → `DAY_TRADE_LIMIT`, `details["remaining"] == 0`, testo con «Tetto operazioni di giornata» e la data di `frees_on`;
    - `test_auto_rules_portfolio_cap_refused` (stesso con `auto_rules_enabled`);
    - `test_link_has_no_shared_budget`: portafogli A (`intraday_enabled`) e B (`auto_rules_enabled`) sullo stesso collegamento `alpaca_paper`, 3 operazioni ciascuno nelle 5 sedute, nessun tetto, conto fresco con `multiplier "4"`, `equity "10000"` → acquisto `day_trade` accettato su entrambi (la «quarta operazione» non esiste più);
    - `test_entry_refused_when_pending_fill_the_cap`: `max_day_trades_5d=3`, 2 usate, posizione di giornata aperta su MSFT → `day_trade` su AAPL rifiutato, su MSFT accettato;
    - `test_buying_power_refusal`: conto fresco con `buying_power "500"`, acquisto `day_trade` da circa 1.000 $ → `INSUFFICIENT_BUYING_POWER`, `details["reason"] == "broker_buying_power"`, testo con «potere d'acquisto del conto»; con `buying_power "5000"` → accettato;
    - `test_day_leverage_capped_at_two_on_paper`: collegamento `alpaca_paper`, portafoglio con `leverage=3` (scritto nel database), patrimonio 10.000 €, nessuna posizione → acquisto `day_trade` da 25.000 € rifiutato con `details["reason"] == "day_trade_leverage"`, `details["leverage"] == "2"`; da 19.000 € accettato; lo stesso acquisto da 25.000 € con `day_trade=False` non è toccato dal passo 10 (lo giudica il passo 9 con leva 3);
    - `test_live_link_day_leverage_one`: collegamento di un tipo in `LIVE_KINDS` (fixture dei test live esistenti), portafoglio con `leverage=2` scritto nel database, patrimonio 10.000 € → acquisto `day_trade` da 15.000 € rifiutato con `reason == "day_trade_leverage"`, `leverage == "1"`;
    - `test_leverage_two_paper_allowed`: `leverage=2` su `alpaca_paper`, acquisto `day_trade` da 19.000 € su 10.000 € di patrimonio → accettato (con `buying_power` fresco sufficiente);
    - `test_stale_or_missing_account_state_no_refusal`: `buying_power "0"` letto 20 minuti fa → accettato; `pdt_state=None` → accettato; stato vecchio `{"daytrade_count": 3, "equity": "1000"}` → accettato;
    - `test_sells_never_refused`: `max_day_trades_5d=0` e `buying_power "0"` fresco → vendita reduce-only accettata;
    - `test_non_day_trade_buy_not_checked`: stessi limiti, `day_trade=False` → accettato;
    - `test_off_portfolio_never_refused` (non regressione, resta): portafoglio spento con `max_day_trades_5d=0` e `buying_power "0"` fresco → accettato, nessuna query in più (contatore di query uguale a prima della modifica);
    - `test_no_outbox_on_day_trade_sells`: portafoglio spento e uno in `day_scope` sullo stesso collegamento, acquisto e vendita oggi → nessuna riga `telegram_outbox` (l'avviso è tolto);
    - `test_preview_reports_day_trade_limit` (resta, con il tetto proprio): `ok=False`, `code == "DAY_TRADE_LIMIT"`.
  - `tests/integration/test_intraday_account_reader.py`: `test_interval` → `ACCOUNT_READ_INTERVAL_S == 300`; `test_reads_only_links_with_day_scope_portfolios` resta con le **6** chiavi esatte; **cancellare** `test_mismatch_alert_once` e `test_day_trader_flag_alert_once_per_session`; **nuovi** `test_blocked_account_alert_once_per_session` (`trading_blocked: true` → una riga outbox `system` con «bloccato dal broker»; secondo giro nella stessa seduta → nessuna; seduta dopo → una), `test_account_blocked_alert` (`account_blocked: true` → una riga), `test_unblocked_no_alert`; `test_read_failure_keeps_old_state` e `test_no_secret_in_state_or_logs` restano (nomi nuovi delle funzioni); `test_realistic_account_payload` riscritto con il JSON di `/v2/account` di oggi (senza `daytrade_count` né `pattern_day_trader`, con `intraday_adjustments`, `buying_power`, `multiplier "4"`, `trading_blocked false`, `account_blocked false`) → stato con le 6 chiavi, nessuna chiave `daytrade_count`, `pattern_day_trader`, `intraday_adjustments`, `account_number`, `id`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare (cancellazioni e rinomine sopra; `grep -rn "pdt_applies\|PDT_MAX\|PDT_EQUITY_MIN\|link_budget\|entry_budget\|warn_day_trade_sell\|daytrade_count\|pattern_day_trader\|intraday\.pdt\|PDT_KINDS\|PDT_READ_INTERVAL_S\|read_pdt_state\|refresh_pdt_states" backend/src backend/tests` → vuoto, salvo il test dello stato vecchio).
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_day_trade_count.py tests/integration/test_intraday_day_positions.py tests/integration/test_intraday_day_trade_check.py tests/integration/test_intraday_account_reader.py tests/integration/test_risk_checks.py tests/integration/test_order_service.py tests/integration/test_admin_orders.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `refactor(intraday): the PDT rule is gone — own day-trade cap, day leverage and broker account state`.

---

### Task 7: Scrittore di compiti Paperclip generalizzato al destinatario

**Files:**
- Modify: `paperclip/issues.py`, `positions/protection.py`
- Create: `tests/unit/test_paperclip_issues.py`
- Test: `tests/integration/test_protection_alarm.py`, `tests/unit/test_protection_eval.py` (invariati, devono restare verdi)

**Interfaces — Consumes (esistenti, non si riscrivono):** `find_agent_id(agents, name=RISK_MANAGER)` (firma **invariata**, con il default: `tests/unit/test_protection_eval.py` righe 179 e 197 lo chiamano senza nome), `PaperclipIssues.open_issue_with_title(title)`, `CLOSED_STATUSES`.
**Produces:**
- `async def resolve_agent(client: PaperclipIssues, name: str) -> str` — `name` obbligatorio, usa `find_agent_id(agents, name)`; l'id, altrimenti `PaperclipError(f"agente «{name}» non trovato")`.
- `PaperclipIssues.open_identifiers(self) -> set[str]` (nuovo) — identificatori (altrimenti id) dei compiti non chiusi (`CLOSED_STATUSES` esclusi), un solo `GET .../issues`.
- `ProtectionAlarm._create_issues` usa `resolve_agent(client, RISK_MANAGER)`: stesso comportamento e stesso testo d'errore di oggi.

- [ ] **Step 1: test falliti:** `test_resolve_agent_by_any_name` (nomi con maiuscole/spazi diversi), `test_resolve_agent_missing_message` («agente «X» non trovato»), `test_open_identifiers_skips_done_and_cancelled`, `test_find_agent_id_default_unchanged` (`find_agent_id(agents)` senza nome trova ancora il Risk Manager), `test_transport_still_refuses_other_writes` (PATCH su un compito → `RuntimeError(WRITE_REFUSED)` prima della rete).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare; nessuna modifica al testo dei compiti «PROTEGGI» né a `open_issue_with_title`.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_paperclip_issues.py tests/unit/test_protection_eval.py tests/integration/test_protection_alarm.py tests/integration/test_admin_paperclip.py` → PASS.
- [ ] **Step 5: commit** `refactor(paperclip): issue writer resolves any recipient agent`.

---

### Task 8: Segnali con selettore e rilevazione degli scatti

**Files:**
- Modify: `signals/service.py`
- Create: `intraday/watch.py`
- Test: `tests/integration/test_signals.py` (**non modificato**: deve restare verde così com'è), `tests/integration/test_intraday_watch.py`

**Interfaces — Consumes:** `strategy_signals`, `_Approved`, `due_bar_close`, `day_close_at`, Redis.
**Produces:**
- `signals.service.Approved = _Approved` (alias pubblico).
- `strategy_signals(session, router, now=None, *, select: Callable[[Approved], Approved | None] | None = None)` — con `select` ogni approvata passa dal selettore prima di leggere i dati (None = esclusa); senza, identico a oggi. Sui timeframe brevi non c'è barra in formazione (`forming.FORMING_TIMEFRAMES`): entrata e uscita sono già sulle barre chiuse.
- `@dataclass(frozen=True) class Trigger: portfolio_id: UUID; ref: str; name: str; symbol: str; rule: Literal["entry","exit"]; timeframe: str; bar_close: datetime; value: Decimal | None; threshold: Decimal | None; price: Decimal | None; rule_text: str; late: bool`.
- `def intraday_selector(portfolios: dict[UUID, Portfolio], tf: Timeframe, exclude: frozenset[tuple[UUID, str]] = frozenset()) -> Callable[[Approved], Approved | None]` — tiene l'approvata solo se il suo timeframe è `tf`, `tf.value` è in `INTRADAY_TIMEFRAMES`, e restringe `portfolios` a quelli in `portfolios` (di giornata, non archiviati) con `tf.value in p.intraday_timeframes`; le coppie (portafoglio, simbolo) in `exclude` (simboli di una regola automatica, Task 17) sono tolte; nessuno → None.
- `STATE_KEY = "intraday:sig:{portfolio_id}:{ref}:{symbol}:{rule}"`, `STATE_TTL_S = 7 * 86400`.
- `async def evaluate_close(session, router, redis, tf: Timeframe, market: str, bar_close: datetime, now: datetime, *, exclude: frozenset[tuple[UUID, str]] = frozenset()) -> list[Trigger]` — portafogli `intraday_enabled` non archiviati; `strategy_signals(..., select=intraday_selector(..., exclude))`; solo strategie con stato `ok` e simboli il cui strumento è nel `market`; valore attuale: entrata = `triggered_closed and position is None and not open_entry`, uscita = `position is not None and exit["triggered"]`; scatto solo se il valore precedente in Redis è `"0"`; chiave mancante → si scrive il valore attuale **senza** scatto; si scrive sempre il valore attuale con `STATE_TTL_S`. `late = rule == "entry" and bar_close >= day_close_at(market, now, p.day_close_minutes)`. `rule_text`: entrata `f"{indicator} {direction} {threshold}"` in italiano («sotto», «sopra», «incrocia al rialzo»), uscita `exit["rule"]`.

- [ ] **Step 1: test falliti** (fake router con barre 15m come in `test_signals.py`, `fakeredis` o la Redis dei test):
  - `test_select_passthrough_matches_golden` (non regressione, in `test_intraday_watch.py`, sostituisce il vecchio test vacuo): sul fixture RSI 15m di due simboli, `strategy_signals(..., select=lambda a: a)` restituisce i valori scritti a mano nel test (stato `"ok"`, l'elenco dei simboli, `triggered_closed` per simbolo, `value` RSI a 2 decimali), non un confronto della funzione con se stessa; `tests/integration/test_signals.py` non compare nel diff del task (`git diff --stat` del commit) e resta verde;
  - `test_selector_keeps_only_intraday_portfolios`: strategia 15m su due portafogli, uno solo di giornata → una sola `StrategySignal`; strategia 1d → esclusa; portafoglio di giornata con `intraday_timeframes=["1h"]` e strategia 15m → esclusa;
  - `test_selector_excludes_pairs`: `exclude={(pid, "AAPL")}` → la strategia resta per MSFT e non per AAPL su quel portafoglio;
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

**Interfaces — Consumes:** `Trigger` (Task 8), `IntradayTrigger` e costanti (Task 2), `PaperclipIssues`, `resolve_agent`, `open_identifiers` (Task 7), `PaperclipIssues.open_issue_with_title` (**esistente**, si usa così com'è), `agents.access.portfolios_with_agents`, `AgentPortfolio`, `paperclip.config.load_config`, `enqueue`.
**Produces:**
- `def wake_title(t: Trigger, portfolio: Portfolio) -> str` → `f"SEGNALE · {portfolio.code or portfolio.name} · {t.symbol} · {'entrata' if t.rule == 'entry' else 'uscita'}"`.
- `def wake_text(t: Trigger, portfolio: Portfolio, zone: ZoneInfo) -> str` — righe: strategia (`name`, `ref`), timeframe, regola (`rule_text`), valore e soglia (`_it` con virgola), prezzo, ora della barra nel fuso dell'utente, ultima riga «Decidi tu se e come operare: SMTrading non ha inviato ordini.». Nessun nome fisso.
- `class Waker: __init__(self, sessionmaker, *, issues_factory: IssuesFactory | None = None, clock=None, tz: str | None = None)`; `async def handle(self, triggers: list[Trigger]) -> None`.
  1. In una transazione: una riga `IntradayTrigger` `pending` per scatto; esito deciso subito, nell'ordine: `late` → `LATE`; portafoglio senza accesso agenti → `TELEGRAM` (Telegram «system» con titolo e testo); `intraday_wake_agent_id` None, agente inattivo o senza `trade` sul portafoglio → `NO_AGENT` + Telegram con lo stesso testo e la riga «nessun agente da svegliare: imposta il destinatario nel portafoglio»; righe `WOKEN` del portafoglio negli ultimi 3600 s `>= intraday_max_wakes_per_hour` → `CAPPED`; il resto resta `pending` per Paperclip.
  2. Senza transazione aperta: Paperclip disattivato/incompleto → `FAILED` + Telegram con lo stesso testo; altrimenti `resolve_agent(client, agent.name)`, `open_identifiers()`; se l'ultimo `issue_ident` della stessa coppia (portafoglio, simbolo) è ancora aperto → `ISSUE_OPEN` con quell'identificatore; altrimenti `create_issue(title, description, assignee, priority="high")` → `WOKEN`; dopo un `timeout` → `open_issue_with_title(title)` e, se trovato, `WOKEN` con quello; errore → `FAILED`, `error` = messaggio breve, Telegram con lo stesso testo.
  3. In una transazione: aggiornamento di `outcome`, `issue_ident`, `error` delle righe `pending`.
  Un `pending` più vecchio di 10 minuti trovato all'avvio di `handle` si chiude con `open_issue_with_title` (trovato → `WOKEN`, altrimenti `FAILED`): nessun secondo compito.

- [ ] **Step 1: test falliti:**
  - unit: `test_title_uses_code_then_name`, `test_text_has_values_and_bar_time` (valore `Decimal("28.5")` → «28,5»; ora 14:45Z → «15:45» Europe/Rome), `test_text_built_from_portfolio_and_agent_data` (sostituisce il vecchio grep del sorgente: portafoglio fittizio `code="ZZT"`, `name="Fittizio Uno"`, strategia `name="Strat Prova Q"` → il titolo contiene «ZZT», il testo contiene «Strat Prova Q»; con `code=None` il titolo contiene «Fittizio Uno»; cambiando i dati fittizi cambiano titolo e testo, nessun'altra parola maiuscola fuori dal modello di testo);
  - integration (fake `issues_factory` come in `test_protection_alarm.py`): `test_woken_creates_one_issue_for_the_configured_agent` (agente fittizio «Agente Prova Xyz»: assegnatario = id Paperclip di quel nome, nient'altro), `test_one_open_issue_per_portfolio_symbol` (secondo scatto su AAPL con compito aperto → `ISSUE_OPEN`, nessuna seconda `create_issue`; dopo che il compito è `done` → nuovo compito), `test_cap_per_hour` (`intraday_max_wakes_per_hour=2`, tre simboli → 2 `WOKEN`, 1 `CAPPED`), `test_portfolio_without_agents_goes_to_telegram` (nessun compito, una riga outbox `system` con lo stesso titolo, esito `TELEGRAM`), `test_agent_downgraded_to_read_is_no_agent`, `test_paperclip_disabled_is_failed_with_telegram`, `test_timeout_adopts_existing_issue` (usa `open_issue_with_title` esistente), `test_stale_pending_adopted_not_duplicated`, `test_late_entry_recorded_without_wake`.
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

**Interfaces — Consumes:** `day_quantities` (Task 4), `day_close_at`, `session_window` (Task 3), `day_scope`/`DAY_SCOPE` (Task 2), `marketdata.quality.is_wide`, `effective_risk_profile`, `run_job_once`, `enqueue`.
**Produces:**
- `close_position(..., limit_price: Decimal | None = None, client_order_id: str | None = None)` e lo stesso in `_close`: con `limit_price` l'ordine reduce-only è `limit` a quel prezzo con **`tif=TimeInForce.day`** (scade a fine seduta: mai un limite che resta la notte), altrimenti `market` come oggi; `client_order_id` passato alla richiesta (idempotenza dell'engine). Default invariati.
- `def close_limit_price(quote: Quote, margin_pct: Decimal, tick_size: Decimal | None) -> tuple[bool, Decimal | None]` — (forbice larga, prezzo): stretta → `(False, None)` (a mercato); larga con `last` → `(True, floor_to_tick(last * (1 - margin_pct/100), tick_size))` con arrotondamento **per difetto** fatto qui (`(p / tick).to_integral_value(ROUND_FLOOR) * tick`; `tick_size` None → 2 decimali per difetto); larga senza `last` → `(True, None)`. Il chiamante passa `instrument.tick_size`.
- `DAY_CLOSE_JOB = "intraday_day_close"`, `DAY_CLOSE_CHECK_JOB = "intraday_day_close_check"`, `CHECK_BEFORE_END = timedelta(minutes=1)`.
- `class DayCloser: __init__(self, sessionmaker, qs, redis, *, clock=None, tz=None)`; `async def tick(self, now: datetime) -> None`:
  - per ogni portafoglio non archiviato in `DAY_SCOPE` (modo A **e** modo B: ultima garanzia dopo l'uscita di fine seduta dello `scalper`) e ogni simbolo di `day_quantities` il cui mercato ha `day_close_at(market, now, p.day_close_minutes) <= now < chiusura` e `market_status(...).is_open`: `run_job_once(DAY_CLOSE_JOB, f"{pid}:{symbol}:{data}")` → quote → `close_limit_price(quote, profilo.day_close_limit_margin_pct or 0.5, instrument.tick_size)` → `close_position(qty=quantità di giornata, source="system", agent_id=None, limit_price=..., client_order_id=f"smt-dayclose-{data:%Y%m%d}-{symbol}")`. Telegram per ogni chiusura: «Chiusura di sicurezza · {nome} · {simbolo}: {qty} in vendita {a mercato | con limite {prezzo} (forbice larga)}, {m} minuti prima della fine seduta. Motivo: posizione di giornata ancora aperta.» più, se resta quantità di ieri, «Restano {n} comprati prima di oggi: riproteggili (stop e obiettivo del simbolo annullati).» Forbice larga senza ultimo scambio: nessun ordine, Telegram «chiusura di sicurezza impossibile: nessun prezzo affidabile».
  - a `chiusura - CHECK_BEFORE_END <= now < chiusura`: `run_job_once(DAY_CLOSE_CHECK_JOB, ...)`; quantità di giornata ancora > 0 → Telegram «chiusura di sicurezza non eseguita · {nome} · {simbolo}: {qty} ancora aperti a 1 minuto dalla fine». Mai un secondo ordine.
  - errore SMTError dell'engine (ordine rifiutato) → Telegram con codice e messaggio.

- [ ] **Step 1: test falliti:**
  - unit: `test_tight_quote_is_market` (bid 100, ask 100,5 → `(False, None)`), `test_wide_quote_uses_last_minus_margin` (bid 95, ask 105, last 100, margine 0,5, tick 0,01 → `(True, Decimal("99.50"))`), `test_limit_floored_to_tick` (last 100,013, margine 0,5, tick 0,01 → `Decimal("99.51")`: 99,512935 per difetto; tick 0,05 → `Decimal("99.50")`), `test_wide_without_last_is_none`;
  - integration: `test_closes_day_qty_at_cutoff` (posizione di giornata 10 AAPL, 2026-12-01 20:50:30Z → un ordine reduce-only `source="system"`, `qty=10`, `client_order_id="smt-dayclose-20261201-AAPL"`, una riga outbox), `test_auto_rules_portfolio_is_covered` (stesso caso su un portafoglio con solo `auto_rules_enabled` → un ordine), `test_safety_limit_is_day_order` (forbice larga → ordine `limit`, `tif == day`, `expires_at` = chiusura della seduta del 1/12 21:00Z; dopo la chiusura l'ordine è scaduto, non a riposo), `test_not_before_cutoff` (20:49Z → nessun ordine), `test_never_with_market_closed` (Thanksgiving 26/11 e 21:00:30Z del 1/12 → nessun ordine), `test_half_day_cutoff` (27/11: 17:50Z chiude, 20:50Z no), `test_mixed_position_sells_only_day_qty` (60 di ieri + 40 oggi → `qty=40`, testo con «Restano 60»), `test_wide_spread_limit_order`, `test_unfilled_alert_at_one_minute` (ordine limite non eseguito → alle 20:59:10Z una riga «non eseguita», nessun secondo ordine), `test_run_once_per_session` (due tick nella finestra → un ordine), `test_off_portfolio_untouched` (non regressione: portafoglio con entrambi gli interruttori spenti con acquisto `day_trade` → nessun ordine), `test_close_position_defaults_unchanged` (non regressione: `close_position` senza nuovi argomenti → ordine `market`, `tif` come oggi, `client_order_id is None`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_intraday_day_close_price.py tests/integration/test_intraday_day_close.py tests/integration/test_order_service.py tests/integration/test_synthetic_orders.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): safety close of day positions before the session end`.

---

### Task 11: Job del worker e riepilogo di giornata

**Files:**
- Create: `intraday/job.py`, `intraday/summary.py`
- Modify: `worker/main.py`
- Test: `tests/integration/test_intraday_job.py`, `tests/unit/test_worker.py` (un test in più)

**Interfaces — Consumes:** `evaluate_close` (Task 8), `Waker` (Task 9), `DayCloser` (Task 10), `refresh_account_states`, `ACCOUNT_READ_INTERVAL_S` (Task 6R), `own_cap_usage` (Task 6R), `due_bar_close`, `session_window` (Task 3), `DAY_SCOPE`, `approved_strategies`, `run_job_once`, `job_tick`.
**Produces:**
- `TICK_S = 15`, `BAR_JOB = "intraday_bar"`, `SUMMARY_JOB = "intraday_summary"`, `SUMMARY_AFTER_CLOSE = timedelta(minutes=5)`.
- `class IntradayJob: __init__(self, sessionmaker, router, redis, http, *, issues_factory=None, clock=None, tz=None, tick_s: float = TICK_S, exclude_provider: Callable[[AsyncSession], Awaitable[frozenset[tuple[UUID, str]]]] | None = None)`; `async def run(self) -> None` (ciclo con `job_tick()`, eccezioni registrate e ciclo che continua, come `ProtectionAlarm.run`); `async def tick(self) -> None`:
  1. una query: portafogli non archiviati in `DAY_SCOPE`; nessuno → ritorno **senza** toccare Redis, router, Paperclip o broker;
  2. solo per i portafogli `intraday_enabled`: coppie (timeframe, mercato): timeframe dall'unione di `intraday_timeframes`, mercati degli strumenti dei simboli delle approvate su quei portafogli (`approved_strategies`, nessun dato di mercato); per ognuna `c = due_bar_close(...)`; `c` non None e `run_job_once(BAR_JOB, f"{tf}:{market}:{c.isoformat()}")` (commit prima del calcolo) → `evaluate_close(..., exclude=await exclude_provider(session) if exclude_provider else frozenset())` → `Waker.handle`;
  3. `DayCloser.tick(now)` (tutti i portafogli in `DAY_SCOPE`);
  4. `refresh_account_states` ogni `ACCOUNT_READ_INTERVAL_S` mentre la seduta `NYSE` è aperta, più una volta nei 10 minuti dopo la chiusura;
  5. a `chiusura + SUMMARY_AFTER_CLOSE`, per portafoglio `intraday_enabled` con scatti nella seduta: `run_job_once(SUMMARY_JOB, f"{pid}:{data}")` → Telegram `day_summary_text`.
- `async def day_summary_text(session, portfolio: Portfolio, day: date) -> str | None` — «Riepilogo di giornata · {nome} · {gg/mm}: {n} scatti — {woken} sveglie, {telegram} solo Telegram, {capped} saltati per tetto, {issue_open} con compito già aperto, {no_agent} senza agente, {late} dopo l'orario, {failed} non riusciti; operazioni di giornata {used} (con tetto: «{used}/{max_day_trades_5d}, la prossima si libera il …», da `own_cap_usage`; nessun riferimento alla regola USA)»; None senza scatti. I sette conteggi sommano a `n` (un `pending` rimasto si conta tra i non riusciti).
- `worker/main.py`: `supervise("intraday", lambda: gated(gate, lambda: IntradayJob(sessionmaker, router, redis, http, exclude_provider=owned_symbols).run()))` (`owned_symbols` dal Task 17; fino ad allora `None`).

- [ ] **Step 1: test falliti:**
  - `test_idle_without_day_scope_portfolios` (non regressione): con portafogli con entrambi gli interruttori spenti, router e Redis finti che falliscono a ogni chiamata → `tick()` non solleva e non li chiama; nessuna riga in `job_runs`, `intraday_triggers`, `telegram_outbox`;
  - `test_auto_rules_only_runs_day_close_not_bars`: solo un portafoglio `auto_rules_enabled` → `DayCloser.tick` chiamato, `evaluate_close` mai;
  - `test_bar_processed_once`: due `tick()` alle 15:01:05Z e 15:01:20Z → `evaluate_close` chiamato una volta per (15m, NYSE, 15:00Z);
  - `test_restart_does_not_reprocess`: nuovo `IntradayJob` sullo stesso database dopo il primo tick → nessuna nuova valutazione della stessa barra;
  - `test_summary_once_after_close`: scatti nella seduta → alle 21:05:30Z una riga outbox col riepilogo; secondo tick → nessuna;
  - `test_summary_counts_add_up`: 7 scatti, uno per esito (`woken, telegram, capped, issue_open, no_agent, late, failed`) → il testo dice «7 scatti» e contiene «1 solo Telegram»; la somma dei sette numeri estratti dal testo è 7;
  - in `tests/unit/test_worker.py`, `test_worker_registers_intraday_behind_gate`: `"intraday"` tra i job avviati da `worker.main.run` (come per gli altri job dietro `gated`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_job.py tests/unit/test_worker.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): worker job (bar closes, day close, account state, summary)`.

---

### Task 12: API admin (modo A e interruttore del modo B)

**Files:**
- Create: `admin/routes_intraday.py`
- Modify: `admin/schemas.py` (`PortfolioPatchIn`, `RiskProfilePatchIn`), `admin/routes_portfolios.py` (`PORTFOLIO_FIELDS`, `patch_portfolio`, `_order_dto`), `admin/routes_orders.py` (`OrderIn.day_trade`), `admin/routes_risk.py`, `admin/routes_signals.py`, `positions/open_view.py`, `api/main.py` (router), `cli.py` (comandi `intraday`)
- Test: `tests/integration/test_admin_intraday.py`, `tests/integration/test_admin_portfolios.py`, `tests/integration/test_admin_orders.py`, `tests/integration/test_admin_signals.py`

**Interfaces — Consumes:** Task 2, 4, 6R (`read_account_state`, `own_cap_usage`, `ACCOUNT_KINDS`, `fresh_buying_power`, `DAY_TRADE_MAX_LEVERAGE`), 8.
**Produces (solo API admin, mai MCP):**
- `PortfolioPatchIn`: `intraday_enabled: StrictBool | None`, **`auto_rules_enabled: StrictBool | None`**, `intraday_timeframes: list[str] | None` (sottoinsieme non vuoto di `INTRADAY_TIMEFRAMES`, senza doppioni), `intraday_wake_agent_id: uuid.UUID | None` (deve avere `permission == "trade"` sul portafoglio ed essere attivo, altrimenti `INVALID_ORDER` «l'agente da svegliare deve avere il permesso trade su questo portafoglio»; `null` lo toglie), `day_close_minutes: int | None` (2-60), `max_day_trades_5d: int | None` (0-100, `null` toglie), `intraday_max_wakes_per_hour: int | None` (1-60). Modifiche solo admin (`require_admin` come gli altri campi sensibili del PATCH); ogni modifica di questi campi → `write_audit(method="admin:portfolio.intraday", ...)`.
- Accensione di `auto_rules_enabled` (da false a true), **prima** di aprire la transazione di scrittura: il portafoglio deve avere un collegamento `enabled` con `kind in ACCOUNT_KINDS`, altrimenti `INVALID_ORDER` «Regole automatiche disponibili solo su portafogli collegati ad Alpaca»; poi `read_account_state(...)` (lettura fresca, nessuna transazione aperta); lettura fallita → `PROVIDER_UNAVAILABLE` «Impossibile leggere il conto {link}: riprova»; conto **in contanti** (`multiplier` assente o ≤ 1) → `INVALID_ORDER` «Regole automatiche non attivabili: il conto {link} è in contanti e le regole di regolamento non permettono di riusare subito il denaro delle vendite; serve un conto con margine»; `trading_blocked` o `account_blocked` → `INVALID_ORDER` «Regole automatiche non attivabili: il conto {link} risulta bloccato dal broker». Nessuna soglia di patrimonio. Lo spegnimento è sempre permesso (le regole attive passano «in pausa», Task 16).
- Leva (punto 6): accendere `intraday_enabled` o `auto_rules_enabled` con `leverage > DAY_TRADE_MAX_LEVERAGE`, o portare `leverage` oltre 2 su un portafoglio in `day_scope` (anche nello stesso PATCH) → `INVALID_ORDER` «Per il lavoro in giornata la leva del portafoglio può essere al massimo 2»; i limiti generali `MIN_LEVERAGE`/`MAX_LEVERAGE` di `_leverage` restano per gli altri portafogli. Sui collegamenti reali nessun rifiuto qui: la leva 1 la impone il passo 10 (Task 6R) e l'armamento reale la chiede già.
- `PORTFOLIO_FIELDS` + i sette campi; `_order_dto` + `day_trade` (`ORDER_FIELDS` MCP invariato).
- `OrderIn.day_trade: bool = False` → `PlaceOrderRequest.day_trade`; anche nell'anteprima.
- `RiskProfilePatchIn.day_close_limit_margin_pct: Num | None` (0 < x ≤ 5), nel DTO admin del profilo.
- `GET /api/intraday/triggers?portfolio_id=&limit=` (default 50, max 200) → `[{id, created_at, portfolio_id, portfolio_name, strategy_ref, strategy_name, symbol, rule, timeframe, bar_close, value, threshold, price, rule_text, outcome, issue_ident, error}]`, più recenti prima.
- `GET /api/intraday/accounts` (al posto di `/api/intraday/pdt`) → per collegamento con almeno un portafoglio: `{link_id, link_name, kind, buying_power, multiplier, equity, trading_blocked, account_blocked, read_at, fresh, margin: bool, portfolios: [{portfolio_id, portfolio_name, intraday_enabled, auto_rules_enabled, leverage, day_leverage, max_day_trades_5d, used, pending, remaining, frees_on}]}` (stato del conto da `pdt_state`, `fresh` = `fresh_buying_power(...) is not None`, `margin` = `multiplier > 1`, `day_leverage` = `day_trade_leverage(...)`; `used/pending/remaining/frees_on` da `own_cap_usage(..., symbol=None)` solo per i portafogli in `day_scope`, `null` per gli altri; nessun ID conto, nessun budget per collegamento).
- `DELETE /api/portfolios/{id}/intraday-triggers` (admin) → `{deleted: n}`, audit `admin:intraday.triggers_delete` (pulizia di fine prova).
- `open_positions`: ogni riga ha `day_qty` (stringa) per i portafogli in `day_scope`, `null` per gli altri (nessuna query in più per questi).
- `/api/signals`: per strategia `intraday: bool` (timeframe in `INTRADAY_TIMEFRAMES` e portafoglio `intraday_enabled`); `CACHE_KEY = "signals:v4"`.
- CLI per la strategia dell'esercitazione (le strategie oggi nascono solo dalle decisioni del CEO via MCP; il portale non le crea): `python -m smtrading.cli intraday drill-strategy --portfolio <id> --agent <nome> --ref <ref> --type <tipo> --params '<json>' --timeframe 15m --symbols A,B` inserisce una `Decision(kind="strategy", agent_id=<id dell'agente indicato>, portfolio_id=<portafoglio>, summary="Esercitazione lavoro in giornata", symbols=[...], details={"ref", "status": "approvata", "strategy", "params", "timeframe"})`; `intraday drill-revoke --portfolio --agent --ref` aggiunge la `revocata`; con `--purge` cancella invece tutte le decisioni di quel portafoglio di prova. Entrambi **rifiutano** un portafoglio con righe `agent_portfolios` o non `intraday_enabled` (la decisione resta invisibile agli agenti: `activity.service._visible` mostra solo i loro portafogli e le globali).

- [ ] **Step 1: test falliti:** `test_patch_intraday_fields_admin_only` (viewer → 403), `test_patch_rejects_bad_timeframes` (`["1d"]`, `[]` → `INVALID_ORDER`), `test_patch_rejects_read_agent`, `test_patch_audited`, `test_auto_rules_refused_on_cash_account` (respx `GET /v2/account` con `multiplier "1"`, `equity "30000"` → 4xx `INVALID_ORDER` con «in contanti», `auto_rules_enabled` resta false, nessuna transazione aperta durante la lettura: la sessione del test non vede lock), `test_auto_rules_refused_on_blocked_account` (`multiplier "4"`, `trading_blocked true` → `INVALID_ORDER` con «bloccato»), `test_auto_rules_allowed_on_small_margin_account` (`multiplier "4"`, `equity "10000"` → ok: nessuna soglia di patrimonio; audit scritto, `pdt_state` con le 6 chiavi dello stato del conto), `test_day_switch_refused_over_leverage_two` (`leverage=3` → accendere `intraday_enabled` o `auto_rules_enabled` → `INVALID_ORDER` «al massimo 2»; portafoglio in `day_scope` con PATCH `leverage=2.5` → stesso rifiuto; `leverage=2` → ok), `test_auto_rules_refused_without_alpaca_link` (portafoglio simulato → `INVALID_ORDER`), `test_auto_rules_read_failure_refused` (HTTP 500 → `PROVIDER_UNAVAILABLE`), `test_auto_rules_disable_always_allowed`, `test_order_day_trade_from_portal` (ordine con `day_trade=true` → DTO admin `day_trade: true`; via MCP `get_order` senza la chiave), `test_triggers_list_and_delete`, `test_accounts_endpoint_shape` (chiavi esatte, nessun `account_id`, nessuna chiave `applies`/`limit` di collegamento/`broker_count`/`mismatch`; `used` del portafoglio in `day_scope` contato, `null` per il portafoglio spento; `day_leverage == "1"` su un collegamento reale), `test_open_positions_day_qty`, `test_signals_intraday_flag`, `test_risk_profile_margin_bounds` (0 e 6 → errore, 0,5 → ok), `test_drill_strategy_refuses_agent_portfolio` (portafoglio con un agente → uscita con errore, nessuna decisione), `test_drill_strategy_is_invisible_to_agents` (dopo l'inserimento `get_decisions` via MCP di un agente qualsiasi non la restituisce; `approved_strategies` la vede sul solo portafoglio di prova), `test_drill_revoke_purge_deletes_decisions`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_admin_intraday.py tests/integration/test_admin_portfolios.py tests/integration/test_admin_orders.py tests/integration/test_admin_signals.py tests/integration/test_mcp_orders.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(intraday): admin API for settings, triggers, account state, day orders and the auto-rules switch`.

---

### Task 13: Portale (modo A e interruttore del modo B)

**Files:**
- Create: `web/src/api/hooks/intraday.ts`
- Modify: `web/src/api/types.ts`, `web/src/pages/PortfolioDetailSettings.tsx`, `web/src/components/RiskProfileCard.tsx` (o `pages/Risk.tsx`, dove si modifica il profilo), `web/src/pages/Signals.tsx`, `web/src/pages/Positions.tsx`, `web/src/components/OrderForm.tsx`
- Test: i `.test.tsx` accanto a ciascun file

**Interfaces — Consumes:** il contratto del Task 12.
**Produces:** `useIntradayTriggers(portfolioId?: string)`, `useIntradayAccounts()`, `useDeleteIntradayTriggers(portfolioId: string)`; tipi `IntradayTrigger`, `IntradayAccount` (con `portfolios: IntradayAccountPortfolio[]`); `Portfolio` + i sette campi; `Order.day_trade`.

- Impostazioni del portafoglio: sezione «Lavoro in giornata» dentro `AdminOnly`: interruttore «Sveglia dell'agente (modo A)», caselle 5m/15m/1h, scelta «Agente da svegliare» tra gli agenti con permesso trade sul portafoglio (vuoto = «nessuno: solo registrati e avviso»), «Chiusura di sicurezza (minuti prima della fine)», «Tetto operazioni di giornata su 5 sedute» (vuoto = nessun tetto), «Sveglie massime all'ora»; interruttore «Regole automatiche (modo B, prove)» con il messaggio d'errore del server mostrato sotto l'interruttore se il rifiuto arriva (conto in contanti, conto bloccato, leva oltre 2). Testo d'aiuto: «Spenti: il portafoglio lavora come oggi. In giornata la leva è al massimo 2 sui conti di prova e 1 sui conti reali.»
- Profilo di rischio: «Margine del limite nella chiusura di sicurezza (%)».
- Segnali: filtro «Solo in giornata» e colonna «In giornata»; tabella «Ultimi scatti» (ora barra, portafoglio, strategia, simbolo, entrata/uscita, valore/soglia, esito in chiaro: «compito aperto», «già un compito aperto», «saltato per tetto», «nessun agente», «solo Telegram», «dopo l'orario», «non riuscito»).
- Posizioni: segno «di giornata» con la quantità quando `day_qty` > 0; riquadro «Conto e operazioni di giornata» per collegamento: «potere d'acquisto X $ · margine ×M · letto alle hh:mm» (con `fresh=false`: «non letto di recente»; con `margin=false`: «conto in contanti»), segno rosso «bloccato dal broker» se `trading_blocked` o `account_blocked`; per ogni portafoglio in giornata «leva in giornata L» e, con tetto, «usate X su N · restano Y · la prossima si libera il gg/mm», senza tetto «usate X (nessun tetto)». Nessun riferimento alla regola USA o ai 25.000 $.
- Modulo ordini manuale: casella «Di giornata» (solo per gli acquisti), inviata come `day_trade`.

- [ ] **Step 1: test falliti (Vitest + msw):** `PortfolioDetailSettings` invia `PATCH` con `{intraday_enabled: true}` e la sezione non compare a un viewer; con risposta 400 al `PATCH {auto_rules_enabled: true}` il testo del server («in contanti») compare e l'interruttore torna spento; `Signals` filtra e mostra «saltato per tetto» e «solo Telegram»; `Positions` mostra «di giornata 40», «potere d'acquisto», «usate 2 su 3 · restano 1» (tetto 3), «usate 1 (nessun tetto)», «bloccato dal broker» con `trading_blocked: true` e «non letto di recente» con `fresh: false`; `OrderForm` invia `day_trade: true` e nasconde la casella su una vendita.
- [ ] **Step 2:** `cd web && npx vitest run src/pages/PortfolioDetailSettings.test.tsx src/pages/Signals.test.tsx src/pages/Positions.test.tsx src/components/OrderForm.test.tsx` → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `cd web && npx vitest run && npm run lint && npm run build` → PASS.
- [ ] **Step 5: commit** `feat(web): intraday settings, triggers, day positions, account state and the auto-rules switch`.

---

### Task 14: Storico a barre brevi

**Files:**
- Test: `tests/integration/test_intraday_history.py` (nessun codice di prodotto, salvo un difetto trovato dai test)

**Interfaces — Consumes:** `MarketDataRouter.get_bars`, `AlpacaProvider.get_bars`, `BacktestRunner`, `fetch_bars`.

- [ ] **Step 1: test** (respx sul fornitore Alpaca, nessuna rete):
  - `test_alpaca_15m_pages_are_joined`: tre pagine con `next_page_token` → tutte le barre, in ordine, `timeframe=15Min`, `feed=iex`;
  - `test_alpaca_1m_session_in_one_call`: una seduta piena a `1Min` (390 barre, due pagine) → 390 barre in ordine, `timeframe=1Min` (serve al riscaldamento e alla prova sullo storico del modo B);
  - `test_intraday_bars_cached_until_next_close`: due `get_bars(15m)` nello stesso quarto d'ora → una richiesta; dopo la chiusura della barra successiva → nuova richiesta (la chiave cambia con `end` arrotondato);
  - `test_backtest_15m_60_days_accepted`: `check_request` con 15m su 60 giorni ok, 91 giorni → `RISK_LIMIT_EXCEEDED` (limite esistente `MAX_INTRADAY_SPAN_DAYS = 90`);
  - `test_watch_fetch_has_enough_bars`: `fetch_bars(15m, 250)` sopra un fine settimana con festività restituisce almeno `min_bars` della strategia RSI 14 della prova.
- [ ] **Step 2:** eseguire; questi test descrivono comportamento esistente e possono passare subito (dichiarato); se un test fallisce per un difetto, correggere nel modulo indicato dal test e annotarlo nel report.
- [ ] **Step 3:** `uv run pytest -q -p no:cacheprovider tests/integration/test_intraday_history.py` → PASS.
- [ ] **Step 4: commit** `test(marketdata): intraday history paging, cache and backtest span`.

---

### Task 15: Modelli delle regole automatiche (puri, condivisi)

**Files:**
- Create: `autorules/__init__.py`, `autorules/indicators.py`, `autorules/models.py`, `autorules/runner.py`
- Test: `tests/unit/test_autorules_models.py`, `tests/unit/test_autorules_runner.py`

**Interfaces — Consumes:** `marketdata.types.Bar`; `intraday.calendar.session_window` (Task 3, per le finestre di seduta; se il Task 3 non è ancora unito si usa `instruments.market_hours.session_on` direttamente). Nessun I/O, nessun database.
**Produces:**
- `indicators.py`:
  - `class MinuteSeries: __init__(self, maxlen: int)`; `add(bar: Bar) -> None` (stessa `ts` dell'ultima → sostituisce; più vecchia → ignorata); `closed(n) -> list[Bar]` (le ultime `n`); `highest(n) -> Decimal | None`; `mean_std(n) -> tuple[Decimal, Decimal] | None` (popolazione, sulle chiusure; `Decimal.sqrt` con contesto a 28 cifre); `avg_volume(n) -> Decimal | None`; `count() -> int`.
  - `class SessionVwap: reset(start: datetime)`; `add(bar: Bar)` (ignora barre prima di `start`); `value -> Decimal | None` = `Σ((h+l+c)/3 × v) / Σ v`, None con volume nullo (Deviazione 5).
- `models.py`:
  - `@dataclass(frozen=True) class Bound: lo: Decimal | int | None; hi: Decimal | int | None; default: Decimal | int | str | None; kind: Literal["dec","int","choice","opt_dec"]; choices: tuple[str, ...] = ()`.
  - `COMMON_PARAMS`: `stop_pct` dec [0.05, 5] def 0.3; `target_pct` dec [0.05, 10] def 0.5; `max_hold_s` int [10, 3600] def 300; `no_entry_first_min` int [0, 60] def 5; `no_entry_last_min` int [0, 120] def 15; `cooldown_s` int [0, 3600] def 60; `entry` choice (`market`, `limit`) def `market`; `limit_offset_pct` dec [0, 0.5] def 0.05; `entry_timeout_s` int [1, 60] def 5; `max_age_s` dec [0.5, 10] def **2**; `max_spread_pct` dec [0.01, 1] def **0.10**.
  - `MODELS: dict[str, ModelSpec]`, `ModelSpec(name, params: dict[str, Bound], warmup: Callable[[dict], int], entry: Callable[[MarketState, Decimal, dict], dict | None])` (l'entrata restituisce i valori dello scatto, per `auto_trades.trigger_values`):
    - `breakout`: `lookback_min` int [5, 240] def 20; `volume_mult` opt_dec [1, 10] def None. Scatto: `price > highest(lookback_min)` delle barre chiuse e (`volume_mult` None o volume dell'ultima barra chiusa `>= volume_mult × avg_volume(lookback_min)`). Valori `{"high", "volume", "avg_volume"}`. Riscaldamento `lookback_min`.
    - `mean_reversion`: `lookback_min` int [10, 240] def 30; `z` dec [1, 5] def 2. Scatto: `price <= mean - z × std` delle chiusure (`std > 0`). Valori `{"mean", "std", "z_now"}`. Riscaldamento `lookback_min`.
    - `vwap_pullback`: `pullback_pct` dec [0.01, 2] def 0.1; `min_above_min` int [1, 60] def 5. Scatto: le ultime `min_above_min` barre chiuse hanno chiusura `> vwap` e `vwap <= price <= vwap × (1 + pullback_pct/100)`. Valori `{"vwap", "distance_pct"}`. Riscaldamento `min_above_min` (più le barre dall'apertura per il VWAP).
  - `def validate_params(model: str, params: dict) -> dict` — riempie i default, rifiuta chiavi sconosciute, tipi sbagliati e valori fuori limite con `SMTError(INVALID_ORDER, f"parametro {k} fuori dai limiti {lo}–{hi}")` (numeri all'italiana nel testo); modello sconosciuto → `INVALID_ORDER` «modello sconosciuto».
  - `def model_catalog() -> list[dict]` — modelli e limiti per il portale (Task 25), dai dati sopra.
  - `@dataclass(frozen=True) class Brakes: max_trades_hour: int | None; max_trades_day: int | None; max_loss_day: Decimal | None; max_loss_day_pct: Decimal | None; max_consecutive_losses: int | None; max_open_positions: int | None`; `RULE_BRAKE_DEFAULTS = Brakes(20, 100, None, Decimal("1"), 3, 1)`, `PORTFOLIO_BRAKE_DEFAULTS = Brakes(60, 300, None, Decimal("2"), 5, 5)` (scelte del piano: lo spec non dà numeri); limiti: ore [1, 120], giorno [1, 1000], perdita > 0, perdita % (0, 50], perdite di fila [1, 20], posizioni [1, 30]; `def validate_brakes(d: dict, *, level: Literal["rule","portfolio"]) -> Brakes` (chiavi mancanti → default del livello; per una regola `max_open_positions` è sempre 1).
- `runner.py` (la stessa macchina a stati per motore e storico):
  - `@dataclass(frozen=True) class Entry: ts; price: Decimal; values: dict`; `@dataclass(frozen=True) class Exit: ts; reason: Literal["time","session"]`.
  - `class RuleRunner: __init__(self, model: str, params: dict, *, market: str, tick_size: Decimal, session_exit_before: timedelta)` (`session_exit_before = (day_close_minutes + 2) minuti`, passato dal chiamante: lo `scalper` esce prima della rete del worker).
  - `on_bar(bar: Bar)` aggiorna serie e VWAP (al cambio di seduta `SessionVwap.reset`; cripto: seduta = giorno UTC).
  - `on_price(ts, price, bid, ask) -> Entry | Exit | None`: stato `flat` → entrata solo se riscaldato, fuori dalle finestre (azioni: `ts >= apertura + no_entry_first_min` e `ts < chiusura - max(no_entry_last_min, session_exit_before)`; cripto: nessuna finestra), dopo `cooldown_s` dall'ultima uscita, e il modello scatta; stato `pending` (entrata inviata) → None; stato `long` → `Exit("session")` se `ts >= chiusura - session_exit_before` (azioni), altrimenti `Exit("time")` se `ts - filled_at >= max_hold_s`, altrimenti None.
  - `sent(ts)`, `entered(ts, fill_price)`, `exited(ts)`, `canceled()` (entrata non eseguita → `flat` senza pausa) aggiornano lo stato; `levels(expected: Decimal) -> tuple[Decimal, Decimal]` = (stop per difetto al tick di `expected × (1 - stop_pct/100)`, obiettivo per eccesso al tick di `expected × (1 + target_pct/100)`).
  - Nessun ordine, nessun orologio interno: il tempo arriva solo dagli argomenti (riproducibile).

- [ ] **Step 1: test falliti:**
  - unit `test_autorules_models.py`: `test_breakout_fires_above_high` (20 barre con massimo 101, prezzo 101,01 → valori con `high == 101`; 101 → None), `test_breakout_volume_filter` (`volume_mult=2`, ultima barra con volume 1,5 × media → None; 2 × → scatto), `test_mean_reversion_fires_below_band` (serie nota con media 100 e std 1, `z=2`: 97,99 → scatto, 98,01 → None; std 0 → None), `test_vwap_pullback` (5 chiusure sopra VWAP 100, prezzo 100,05 con `pullback_pct=0.1` → scatto; 99,99 → None; 100,2 → None; una delle 5 sotto → None), `test_session_vwap_typical_price` (due barre (h,l,c,v) = (102,98,100,10) e (104,100,102,30) → VWAP `(100×10 + 102×30)/40 = 101,5`), `test_validate_params_defaults_and_bounds` (vuoto → tutti i default; `stop_pct=0.01` → errore con «stop_pct fuori dai limiti»; chiave `foo` → errore; `lookback_min=4` → errore), `test_validate_brakes_levels` (regola senza chiavi → `RULE_BRAKE_DEFAULTS`; regola con `max_open_positions=3` → 1; portafoglio con `max_loss_day_pct=60` → errore), `test_model_catalog_lists_three_models`;
  - unit `test_autorules_runner.py`: `test_no_entry_before_warmup`, `test_no_entry_in_first_minutes` (azioni NYSE, 2026-12-01 14:33Z con `no_entry_first_min=5` → None; 14:35Z → Entry), `test_no_entry_in_last_window` (`no_entry_last_min=15`, `session_exit_before=12 min`: 20:44:59Z ok, 20:45Z None), `test_cooldown_after_exit` (`cooldown_s=60`: dopo `exited` a t, nessuna entrata fino a t+60 s), `test_pending_blocks_second_entry`, `test_time_exit` (`max_hold_s=300`: Exit("time") a filled_at+300 s, non prima), `test_session_exit_before_worker_net` (`day_close_minutes=10` → Exit("session") alle 20:48Z del 1/12, sulla mezza giornata del 27/11 alle 17:48Z), `test_crypto_has_no_windows_or_session_exit` (BTC-USD alle 03:00Z → Entry; nessun Exit("session")), `test_levels_rounded_to_tick` (expected 100,013, stop 0,3, target 0,5, tick 0,01 → `(Decimal("99.71"), Decimal("100.52"))`), `test_canceled_entry_returns_flat_without_cooldown`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_autorules_models.py tests/unit/test_autorules_runner.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): parameterised models and the shared rule state machine`.

---

### Task 16: Schema e gestione delle regole automatiche

**Files:**
- Create: `migrations/versions/0047_auto_rules.py`, `autorules/tables.py`, `autorules/rules.py`
- Modify: `models.py` (registro), `engine/models.py` (`Order.auto_rule_id`), `portfolios/models.py` (`auto_brakes`), `marketdata/streaming.py` (solo `SOURCES` + `"auto_rules"`), `admin/routes_portfolios.py` (`pause_all` allo spegnimento di `auto_rules_enabled`)
- Test: `tests/integration/test_migrations.py` (drift), `tests/integration/test_autorules_schema.py`

**Interfaces — Consumes:** `validate_params`, `validate_brakes`, `MODELS` (Task 15), `marketdata.streaming.track/untrack`, `signals.service.approved_strategies`, `INTRADAY_TIMEFRAMES`.
**Produces:**
- Migrazione `0047_auto_rules.py` (`down_revision` = revisione di `0046`):
  - `portfolios.auto_brakes JSONB not null server_default '{}'`;
  - `auto_rules`: `id UUID pk; portfolio_id UUID FK portfolios ON DELETE CASCADE; symbol String(32) FK instruments.symbol; model String(32)` CHECK in `('breakout','mean_reversion','vwap_pullback')`; `status String(16)` CHECK in `('draft','active','paused','braked')`; `version int not null default 1; status_reason String(200)|None; status_changed_at timestamptz; created_at, updated_at timestamptz server_default now()`. Indice unico parziale `uq_auto_rules_active_symbol ON (portfolio_id, symbol) WHERE status = 'active'`; indice `(status)`;
  - `auto_rule_versions`: PK `(rule_id, version)`, `rule_id FK auto_rules ON DELETE CASCADE; params JSONB not null; qty NUM|None; notional NUM|None` CHECK esattamente uno dei due e `> 0`; `brakes JSONB not null; created_at; created_by String(64)`;
  - `orders.auto_rule_id UUID|None FK auto_rules ON DELETE SET NULL`, indice;
  - `auto_trades`: `id BIGINT identity; rule_id UUID FK auto_rules ON DELETE CASCADE; rule_version int; portfolio_id UUID FK portfolios ON DELETE CASCADE; symbol String(32); trigger_key String(64)`, **UNIQUE `(rule_id, trigger_key)`**; `trigger_at timestamptz; trigger_price NUM; trigger_bid NUM|None; trigger_ask NUM|None; trigger_values JSONB; expected_price NUM; status String(16)` CHECK in `('sent','open','closed','canceled','rejected')`; `entry_order_id UUID|None FK orders ON DELETE SET NULL; sent_at timestamptz|None; broker_sent_at|None; filled_at|None; fill_ts_source String(8)|None` (`broker`/`local`); `fill_price NUM|None; qty NUM|None; exit_order_id UUID|None FK orders ON DELETE SET NULL; exit_reason String(16)|None` CHECK in `('stop','target','time','session','manual','day_close')`; `exit_trigger_at|None; exit_expected NUM|None; exit_filled_at|None; exit_price NUM|None; fees NUM not null default 0; fees_currency String(16)|None; net_result NUM|None; net_currency String(16)|None; entry_slippage_bps NUM|None; exit_slippage_bps NUM|None; ms_trigger_to_send int|None; ms_send_to_broker int|None; ms_broker_to_fill int|None; error String(200)|None; created_at`. Indici `(portfolio_id, trigger_at)`, `(rule_id, status)`.
- `autorules/tables.py`: modelli `AutoRule`, `AutoRuleVersion`, `AutoTrade`; costanti `DRAFT, ACTIVE, PAUSED, BRAKED`, stati e motivi d'uscita come sopra; `STATUS_IT = {"draft": "bozza", "active": "attiva", "paused": "in pausa", "braked": "fermata dai freni"}`.
- `marketdata/streaming.py`: `SOURCES` + `"auto_rules"` (nessun altro cambio in questo task).
- `autorules/rules.py` (ogni funzione nella transazione del chiamante; effetti Redis dopo il commit con `queue_track`/`queue_untrack` se esistono nell'engine, altrimenti un elenco restituito al chiamante che li applica dopo il commit):
  - `async def create_rule(session, *, portfolio_id, symbol, model, params, qty=None, notional=None, brakes=None, by: str) -> AutoRule` — stato `draft`, versione 1, parametri e freni validati; strumento attivo `us_equity` o `crypto`, altrimenti `INVALID_ORDER` «solo azioni USA e cripto».
  - `async def update_rule(session, rule_id, *, params=None, qty=None, notional=None, brakes=None, by) -> AutoRule` — nuova riga `auto_rule_versions` con `version + 1`; su una regola `active` → `INVALID_ORDER` «metti in pausa la regola prima di modificarla».
  - `async def activate(session, rule_id, *, by, now) -> AutoRule` — controlli, nell'ordine, ognuno con il suo testo italiano: portafoglio non archiviato e `auto_rules_enabled`; nessun'altra regola `active` sullo stesso (portafoglio, simbolo) (l'indice unico è la garanzia, il controllo dà il testo); nessuna posizione e nessun ordine aperto del portafoglio su quel simbolo che non sia della regola; simbolo non in una strategia approvata con timeframe in `INTRADAY_TIMEFRAMES` dello stesso portafoglio quando `intraday_enabled` («il simbolo è già seguito dal lavoro in giornata con l'agente su questo portafoglio»). Poi `status=active`, `track([symbol], source="auto_rules")` dopo il commit.
  - `pause(session, rule_id, *, by, reason=None)`, `resume(session, rule_id, *, by, now)` (da `paused` o `braked` con gli stessi controlli di `activate`), `brake(session, rule_id, reason: str)` (`active → braked`, `status_reason`); uscendo da `active`, `untrack([symbol], source="auto_rules")` se nessun'altra regola attiva ha quel simbolo.
  - `async def pause_all(session, portfolio_id, *, by, reason)` — chiamata dal PATCH quando `auto_rules_enabled` si spegne (Task 12 la usa dal Task 16 in poi: aggiungere la chiamata in `patch_portfolio` in questo task).
  - `@dataclass(frozen=True) class ActiveRule: rule_id; version; portfolio_id; symbol; model; params; qty; notional; brakes: Brakes; portfolio_brakes: Brakes; market; asset_class; tick_size; day_close_minutes; base_currency`; `async def active_rules(session) -> list[ActiveRule]` — una query (regole `active` ⋈ versione corrente ⋈ portafogli `auto_rules_enabled` non archiviati ⋈ strumenti).

- [ ] **Step 1: test falliti:**
  - `test_migration_matches_models` (drift) rosso; `test_downgrade_0047_and_back` (`downgrade 0046` → `upgrade head`);
  - `test_one_active_rule_per_symbol`: due regole su (P, AAPL); attivata la prima, l'attivazione della seconda → `INVALID_ORDER`; un `UPDATE` diretto a `active` → IntegrityError (indice parziale); una in `paused` e una `active` convivono;
  - `test_trigger_key_unique`: due `AutoTrade` con stesso `(rule_id, trigger_key)` → IntegrityError;
  - `test_versions_on_update`: `create_rule` → versione 1; `update_rule(params=...)` → versione 2 con i nuovi parametri, la 1 invariata; update su `active` → errore;
  - `test_activate_refuses`: portafoglio con `auto_rules_enabled=False`; posizione aperta su AAPL; ordine a riposo su AAPL; AAPL in una strategia 15m approvata con `intraday_enabled=True` → quattro rifiuti con i testi attesi; strumento FX → rifiuto alla creazione;
  - `test_activate_tracks_symbol_and_pause_untracks`: dopo `activate` + commit `md:tracked:auto_rules` contiene AAPL; dopo `pause` non più; con una seconda regola attiva su AAPL in un altro portafoglio resta;
  - `test_disable_switch_pauses_rules`: `PATCH auto_rules_enabled=false` → tutte le regole del portafoglio `paused` con `status_reason` «interruttore spento»;
  - `test_active_rules_single_query` (contatore di query = 1) e solo regole di portafogli accesi;
  - `test_order_auto_rule_id_default_null` (non regressione).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_migrations.py tests/integration/test_autorules_schema.py tests/integration/test_streaming.py tests/integration/test_admin_portfolios.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): rules, versions and trades schema (0047)`.

---

### Task 17: Un solo proprietario per (portafoglio, simbolo)

**Files:**
- Create: `autorules/ownership.py`
- Modify: `engine/service.py` (`PlaceOrderRequest.auto_rule_id`, `_persist`, `_same_params`, `_run_check`), `risk/checks.py` (passo 11), `worker/main.py` (`exclude_provider=owned_symbols`)
- Test: `tests/integration/test_autorules_ownership.py`

**Interfaces — Consumes:** `AutoRule`, `AutoTrade` (Task 16), `pre_trade_check` (Task 5), `IntradayJob(exclude_provider=)` (Task 11).
**Produces:**
- `PlaceOrderRequest.auto_rule_id: UUID | None = None` → `Order.auto_rule_id` in `_persist`; `_same_params` lo confronta.
- `async def owner_rule(session, portfolio_id, symbol) -> UUID | None` — la regola `active` su (portafoglio, simbolo), altrimenti la regola di un'`AutoTrade` in stato `sent`/`open` su quella coppia; None.
- `pre_trade_check(..., auto_rule_id: UUID | None = None)`; **passo 11** in `_run` (`# 11: symbol owned by an automatic rule`), solo se `portfolio.auto_rules_enabled` (nessuna query altrimenti) e l'ordine **non** è reduce-only: `owner = await owner_rule(...)`; `owner is not None and owner != auto_rule_id` → `_Reject(ErrorCode.RISK_LIMIT_EXCEEDED, «{symbol} è gestito da una regola automatica attiva su questo portafoglio: mettila in pausa prima di operare a mano», {"reason": "auto_rule_owner", "rule_id": owner})`. Un ordine con `auto_rule_id` di una regola non `active` (e senza operazione aperta) → stesso rifiuto con `reason: "auto_rule_inactive"`.
- `async def owned_symbols(session) -> frozenset[tuple[UUID, str]]` — coppie con regola `active`, solo portafogli `intraday_enabled and auto_rules_enabled` (una query); passata come `exclude_provider` a `IntradayJob`.

- [ ] **Step 1: test falliti:**
  - `test_manual_buy_refused_while_rule_active` (ordine del portale `source="admin"` su AAPL con regola attiva → `RISK_LIMIT_EXCEEDED`, `details.reason == "auto_rule_owner"`, testo «gestito da una regola automatica»);
  - `test_agent_buy_refused_while_rule_active` (stesso con `source="agent"`);
  - `test_rule_order_accepted` (stesso ordine con `auto_rule_id` della regola → accettato);
  - `test_reduce_only_never_refused` (vendita reduce-only e `close_position` dal portale con regola attiva → accettati);
  - `test_open_trade_keeps_ownership_after_pause` (regola in pausa con `AutoTrade` `open` → acquisto manuale rifiutato; dopo `closed` → accettato);
  - `test_inactive_rule_order_refused` (`auto_rule_id` di una regola `paused` senza operazioni → `reason == "auto_rule_inactive"`);
  - `test_no_query_when_switch_off` (non regressione: portafoglio con `auto_rules_enabled=False` → nessuna query su `auto_rules`, contatore di query);
  - `test_mode_a_skips_owned_symbol` (portafoglio con entrambi gli interruttori, strategia 15m su AAPL e MSFT, regola attiva su AAPL → `IntradayJob.tick` alla chiusura della barra valuta solo MSFT per quel portafoglio).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_autorules_ownership.py tests/integration/test_risk_checks.py tests/integration/test_order_service.py tests/integration/test_intraday_job.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): one owner per portfolio and symbol across both modes and manual orders`.

---

### Task 18: Barre da 1 minuto sul flusso Alpaca esistente e quota riservata

**Files:**
- Create: `marketdata/minute_bars.py`
- Modify: `marketdata/provider.py` (firma di `stream`, `supports_stream_bars`), `marketdata/providers/alpaca.py` (abbonamento `bars`, parser), `marketdata/wscoalesce.py` (chiave di coalescenza), `marketdata/streaming.py` (ordine con quota, barre per stream, pubblicazione, ripiego senza barre)
- Test: `tests/unit/test_ws_coalesce.py` (+), `tests/unit/test_alpaca_ws_bars.py`, `tests/integration/test_streaming.py` (+)

**Interfaces — Consumes:** `StreamManager`, `tracked_sources`, `AlpacaProvider.stream`, `coalesced`, source `auto_rules` (Task 16).
**Produces:**
- `minute_bars.py`: `@dataclass(frozen=True) class StreamBar: symbol: str; bar: Bar`; `BARS_KEY = "bars1m:{symbol}"` (Redis stream), `BARS_MAXLEN = 1000`; `async def publish_minute_bar(redis, sb: StreamBar) -> None` (`XADD` con `MAXLEN ~ 1000`, campi `ts, o, h, l, c, v` come stringhe; stessa `ts` dell'ultima voce → nessuna nuova voce); `async def read_minute_bars(redis, symbol, *, since_id: str = "0-0", count: int = 1000) -> list[tuple[str, Bar]]`.
- `DataProvider.supports_stream_bars: bool = False`; `DataProvider.stream(self, items, *, bars: frozenset[str] = frozenset())` — gli altri fornitori accettano e ignorano il parametro; `AlpacaProvider.supports_stream_bars = True`.
- `AlpacaProvider.stream`: con `bars` non vuoto il messaggio di abbonamento aggiunge `"bars": [simboli Alpaca]` (solo simboli già in `quotes`/`trades`); `def parse_alpaca_bars(msgs, reverse) -> list[StreamBar]` per i messaggi `T == "b"` (`t, o, h, l, c, v`, `Decimal`); `handle` restituisce quotazioni e barre.
- `coalesced`: chiave di `pending` = `quote.symbol` per `Quote`, `("b", sb.symbol, sb.bar.ts)` per `StreamBar` (le barre non si perdono mai per coalescenza); comportamento per le quotazioni invariato.
- `StreamManager`:
  - `AUTO_RULES_RESERVE = int(os.environ.get("SMT_AUTO_RULES_STREAM_RESERVE", "10"))`.
  - ordine nel tetto (sostituisce `rank` in `reconcile`), Deviazione 2:
    ```python
    auto_only = sorted(s for s, src in sources.items() if set(src) == {"auto_rules"} or set(src) == {"auto_rules", "watchlists"})
    reserved = set(auto_only[:AUTO_RULES_RESERVE])
    def rank(symbol):
        src = set(sources.get(symbol, []))
        if src - {"watchlists", "auto_rules"}: return (0, symbol)   # ordini, posizioni, avvisi, portale
        if symbol in reserved:                 return (1, symbol)   # quota delle regole
        if "watchlists" in src:                return (2, symbol)   # solo liste
        return (3, symbol)                                          # regole oltre la quota: posti liberi
    ```
    Senza simboli `auto_rules` l'ordine relativo è identico a oggi (0 prima di 2).
  - `_Stream` ha `bars: tuple[str, ...]` (simboli Alpaca dei simboli `auto_rules` presenti in `items`); `wanted` confronta `(items, bars)`, quindi il socket si riavvia solo quando cambiano i simboli o l'insieme delle barre (attivazione/pausa di una regola), mai negli altri casi; `bars` passato solo se `provider.supports_stream_bars` e non rifiutato.
  - in `_run_stream`: `StreamBar` → `publish_minute_bar` (fuori dal controllo di qualità, nessuna `write_quote`), le quotazioni come oggi.
  - `StreamSymbolLimit` con `bars` non vuoto → `self._bars_refused[key] = True`, nota «Barre al minuto rifiutate dal fornitore: regole automatiche senza volume e VWAP», nessun dimezzamento; il riavvio successivo è senza barre (Deviazione 4).
  - `status()` aggiunge per stream `"bars": [...]` (chiave nuova, letta da `stream_state` del Task 20).

- [ ] **Step 1: test falliti:**
  - unit `test_ws_coalesce.py`: `test_bars_never_coalesced` (due barre dello stesso simbolo con `ts` diverse e tre quotazioni nello stesso lotto → escono entrambe le barre e l'ultima quotazione); `test_quotes_coalescing_unchanged` (il test esistente resta verde);
  - unit `test_alpaca_ws_bars.py`: `test_subscribe_message_with_bars` (fake websocket: con `bars={"AAPL"}` il messaggio dopo l'autenticazione è `{"action":"subscribe","quotes":[...],"trades":[...],"bars":["AAPL"]}`; senza, identico a oggi, chiave `bars` assente), `test_parse_bar_frame` (`[{"T":"b","S":"AAPL","t":"2026-12-01T15:00:00Z","o":1,"h":2,"l":0.5,"c":1.5,"v":100}]` → `StreamBar("AAPL", Bar(...))` con `Decimal`), `test_crypto_bar_symbol_mapped` (`BTC/USD` → `BTC-USD`);
  - integration `test_streaming.py`: `test_no_auto_rules_same_subscription` (Review Focus 3: 35 simboli tra ordini/posizioni/liste, nessun `auto_rules` → stessi `items` e stesso insieme in polling del codice di oggi, calcolati con la funzione `rank` precedente copiata nel test; nessuna chiave `bars` nel messaggio), `test_reserve_never_displaces_campaign` (Review Focus 3: tetto 30, 25 simboli di ordini/posizioni, 8 solo liste, 12 `auto_rules` → tutti i 25 nello stream, 5 delle regole nello stream (quota 10 limitata dai posti non della campagna), le liste in polling; con 30 simboli di ordini nessuna regola entra), `test_reserve_takes_watchlist_slots` (tetto 30, 15 ordini, 20 liste, 12 regole → 15 + 10 regole + 5 liste; 2 regole in polling), `test_bars_only_for_rule_symbols` (barre chieste solo per i simboli `auto_rules` nello stream), `test_minute_bar_published` (una `StreamBar` dal fake → una voce in `bars1m:AAPL`, nessuna in `quote:AAPL`), `test_bar_refusal_retries_without_bars` (Review Focus 3: `StreamSymbolLimit` con barre → nuovo socket con gli **stessi** `items` e senza barre, nota nello stato, `_caps` invariato), `test_socket_not_restarted_when_bars_unchanged`.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_ws_coalesce.py tests/unit/test_alpaca_ws_bars.py tests/integration/test_streaming.py tests/integration/test_processor.py` → PASS.
- [ ] **Step 5: commit** `feat(marketdata): minute bars on the existing Alpaca stream for rule symbols, with a reserved quota`.

---

### Task 19: Sveglia di BrokerSync (scrittore unico, invio sotto il secondo)

**Files:**
- Modify: `brokers/sync.py`
- Test: `tests/integration/test_brokers_sync_wake.py`, `tests/integration/test_brokers_sync.py` (invariato, deve restare verde)

**Interfaces — Consumes:** `BrokerSync._run`, `tick`, `_step`, Redis pub/sub.
**Produces:**
- `WAKE_CHANNEL = "brokersync:wake"`, `HEARTBEAT_KEY = "brokersync:last_pass"` (ISO UTC, TTL 30 s, scritto alla fine di ogni passaggio di chi tiene il lock), `HOT_POLL_S = 0.5`, `HOT_FOR_S = 10`, `HOT_MAX = 4`.
- `async def wake_broker_sync(redis, order_id: UUID) -> None` — `PUBLISH brokersync:wake <id>`; da chiamare **dopo** il commit.
- In `_run`, al posto di `await asyncio.sleep(self._interval_s)` con il lock tenuto: `ids = await self._wait(self._interval_s)` — attende fino a `interval_s` un messaggio su `WAKE_CHANNEL` (abbonamento aperto una volta su una connessione Redis dedicata; errore di Redis → `sleep` come oggi); con id ricevuti esegue `await self.tick_orders(ids)` e torna ad aspettare il **resto** dell'intervallo, così il passaggio completo resta ogni `interval_s`. Senza messaggi la cadenza è identica a oggi. Chi non tiene il lock ignora i messaggi.
- `async def tick_orders(self, ids: list[UUID]) -> None` — `_step` sui soli ordini indicati ancora in `_SYNCED` (stessa logica, stesse transazioni brevi, stesso task: nessuna concorrenza con il passaggio completo); gli ordini con `auto_rule_id` inviati qui entrano in un insieme «caldi» riletti ogni `HOT_POLL_S` per `HOT_FOR_S` (al massimo `HOT_MAX` alla volta, per il limite di richieste del broker), poi solo nel passaggio normale.

- [ ] **Step 1: test falliti** (fake adapter Alpaca come in `test_brokers_sync.py`, Redis dei test):
  - `test_wake_sends_within_300ms` (ordine `to_send` creato e svegliato a t0 con `interval_s=2` → `submit` del fake chiamato prima di t0 + 0,3 s);
  - `test_no_wake_same_cadence` (non regressione: senza messaggi due passaggi a distanza `interval_s`, nessuna chiamata in più al fake);
  - `test_wake_ignored_without_lock` (secondo `BrokerSync` senza lock: nessuna chiamata al broker per l'ordine svegliato);
  - `test_wake_unknown_or_final_order_ignored`;
  - `test_hot_order_fill_seen_fast` (ordine automatico eseguito dal fake 0,6 s dopo l'invio → riempimento registrato prima di 1,5 s; un ordine senza `auto_rule_id` non è «caldo»);
  - `test_heartbeat_written_each_pass` (`brokersync:last_pass` aggiornato; non scritto da chi non ha il lock);
  - `test_redis_down_falls_back_to_sleep` (pub/sub che solleva → i passaggi continuano ogni `interval_s`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_brokers_sync_wake.py tests/integration/test_brokers_sync.py tests/integration/test_live_sync.py` → PASS.
- [ ] **Step 5: commit** `feat(brokers): wake BrokerSync on demand for automatic orders, single writer kept`.

---

### Task 20: Servizio `scalper`: prezzi, riscaldamento, regole attive

**Files:**
- Create: `autorules/quotes.py`, `autorules/feed.py`, `autorules/service.py`, `autorules/main.py`
- Modify: `docker-compose.yml` (servizio `scalper`)
- Test: `tests/integration/test_scalper_feed.py`

**Interfaces — Consumes:** `active_rules`, `ActiveRule` (Task 16), `RuleRunner` (Task 15), `read_minute_bars`, `BARS_KEY` (Task 18), `marketdata.cache.read_quote_meta`, canale `ticks:{symbol}` di `write_quote`, `STATUS_KEY` dello `StreamManager`, `MarketDataRouter.get_bars` (solo REST per il riscaldamento; limite di richieste condiviso in Redis), `worker.jobstate.JobRecorder`.
**Produces:**
- `class CacheQuoteSource(QuoteSource)`: `get_quotes` legge **solo** la cache Redis (`read_quote_meta`), mai i fornitori (un simbolo senza quota in cache manca dal risultato); `get_fx_rate` e `get_calendar` delegati al router (nessun broker). Usata dall'engine nello `scalper`.
- `def stream_state(status: dict | None, symbol: str) -> Literal["stream","polling","none"]` — dallo stato pubblicato dal gestore (`md:stream_status`): nel `symbols` di uno stream `connected` → `stream`; in `polling` → `polling`; altrimenti `none` (stato assente/scaduto = worker fermo).
- `class PriceFeed: __init__(self, redis, router, *, clock)`; `async def set_symbols(self, symbols: set[str])` (abbonamento pub/sub ai soli `ticks:{s}` delle regole attive; lettura `XREAD BLOCK 1000` su `bars1m:{s}` dall'ultimo id visto); `async def warm_up(self, rule: ActiveRule, runner: RuleRunner, now)` — `router.get_bars(symbol, 1m, start, now)` con `start` = apertura della seduta (azioni) o mezzanotte UTC (cripto), almeno `warmup + 5` minuti prima; poi le voci di `bars1m:{symbol}` più recenti dell'ultima barra; ogni barra va a `runner.on_bar`; **il riscaldamento non chiama mai `on_price`**; `async def events(self) -> AsyncIterator[Quote | StreamBar]`.
- `class Scalper: __init__(self, sessionmaker, redis, router, *, clock=None, reload_s: float = 5, executor_factory=None)`; `async def run(self)`; `async def reload(self)`: una query `active_rules`; nessuna regola → disiscrive tutto, nessuna chiamata a router o Redis oltre la query e il battito; regola nuova o versione cambiata → nuovo `RuleRunner` + `warm_up`; regola tolta → runner scartato (le operazioni aperte restano al `Tracker`, Task 22). `async def on_event(ev)`: barra → `on_bar` dei runner del simbolo; quota → `on_price` dei runner del simbolo, l'intento va all'`Executor` (Task 21). Battito `JobRecorder` col nome «scalper» per «Salute del sistema», con il numero di regole attive.
- `autorules/main.py`: `python -m smtrading.autorules.main` — stesse impostazioni, logging e `StartupGate` del worker; **non** crea `StreamManager`, `OrderProcessor`, `BrokerSync` né altri job.
- `docker-compose.yml`: servizio `scalper` (`<<: *app`, `command: ["python", "-m", "smtrading.autorules.main"]`), nessuna porta.

- [ ] **Step 1: test falliti:**
  - `test_idle_without_active_rules` (non regressione: router e Redis finti che falliscono a ogni chiamata tranne la query e il battito → `reload` e 3 giri del ciclo non sollevano, nessun abbonamento, nessuna `get_bars`);
  - `test_warmup_never_fires` (Review Focus 1: riscaldamento con 60 barre di cui l'ultima sopra il massimo di breakout → nessun intento, nessuna riga `auto_trades`, nessun ordine; la prima quota successiva sopra il massimo → un intento);
  - `test_warmup_merges_history_and_stream` (REST fino a 15:10Z, `bars1m` con 15:09Z e 15:11Z → serie senza doppioni, ultima 15:11Z);
  - `test_reload_picks_new_version` (versione 2 → nuovo runner con i nuovi parametri, il vecchio scartato);
  - `test_cache_quote_source_never_fetches` (simbolo assente in cache → risultato senza il simbolo, il fornitore finto non è chiamato);
  - `test_stream_state` (stato con AAPL in uno stream `connected` → `stream`; in `polling` → `polling`; stato assente → `none`);
  - `test_scalper_never_opens_streams` (durante `run` per 1 s con una regola attiva: `provider.stream` mai chiamato, nessun `StreamManager` creato);
  - `test_compose_has_scalper_service` (lettura di `docker-compose.yml`: servizio `scalper` con il comando atteso e nessun `ports`).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_scalper_feed.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): scalper service reading the existing price feed, with warm-up`.

---

### Task 21: Guardie prima dell'invio e ordine d'entrata

**Files:**
- Create: `autorules/guards.py`, `autorules/executor.py`
- Modify: `autorules/service.py` (collegamento runner → executor)
- Test: `tests/integration/test_scalper_entry.py`

**Interfaces — Consumes:** `RuleRunner`, `Entry` (Task 15), `ActiveRule`, `AutoTrade` (Task 16), `stream_state`, `CacheQuoteSource` (Task 20), `place_bracket_order`, `PlaceOrderRequest(day_trade=, auto_rule_id=)` (Task 2, 17), `wake_broker_sync`, `HEARTBEAT_KEY` (Task 19), `market_status`.
**Produces:**
- `guards.py`: motivi `STALE = "stale"`, `SPREAD = "spread"`, `CLOSED = "closed"`, `BUSY = "busy"`, `NO_STREAM = "no_stream"`, `BROKER_DOWN = "broker_down"`, `BRAKED = "braked"`; `def check_entry(*, quote: Quote, now: datetime, params: dict, market_open: bool, busy: bool, stream: str, broker_last_pass: datetime | None) -> str | None` — nell'ordine: `not market_open` → `CLOSED`; `stream != "stream"` → `NO_STREAM`; `broker_last_pass is None or now - broker_last_pass > 10 s` → `BROKER_DOWN`; `now - quote.ts > max_age_s` → `STALE` (età misurata dall'ora dell'evento della quota); `bid`/`ask` mancanti o `(ask - bid) / mid × 100 > max_spread_pct` → `SPREAD`; quota `suspect` → `STALE`; `busy` (operazione della regola `sent`/`open` o ordine aperto con quell'`auto_rule_id`) → `BUSY`; altrimenti None. Ogni blocco incrementa `HINCRBY autorules:blocked:{rule_id}:{yyyymmdd} {motivo} 1` (TTL 3 giorni) per il riepilogo; nessuna riga.
- `class Executor: __init__(self, sessionmaker, redis, qs: CacheQuoteSource, *, clock)`; `async def enter(self, rule: ActiveRule, runner: RuleRunner, entry: Entry, quote: Quote) -> AutoTrade | None`:
  1. guardie (`busy` letto dal database con una query sulla coppia (regola, stati aperti));
  2. `trigger_key = f"v{rule.version}:{int(entry.ts.timestamp() * 1_000_000)}"`; `client_order_id = f"smt-auto-{rule.rule_id.hex[:12]}-{sha1(trigger_key)[:12]}"`;
  3. `expected = quote.ask`; quantità = `rule.qty` oppure `floor(rule.notional / expected, lot_size)` (cripto: passo dello strumento); zero → nessun ordine, motivo `size` contato;
  4. `stop, target = runner.levels(expected)`; richiesta `PlaceOrderRequest(portfolio_id, symbol, side=buy, type=market | limit (prezzo = expected × (1 + limit_offset_pct/100) per eccesso al tick), tif=day (azioni) | gtc (cripto), qty, source="system", agent_id=None, client_order_id, day_trade=(asset_class == us_equity), auto_rule_id=rule.rule_id)`;
  5. in **una** transazione: riga `AutoTrade(status="sent", trigger_*, expected_price, sent_at=now)` e `place_bracket_order(session, qs, req, take_profit=target, stop_loss=stop, redis=redis)`; `IntegrityError` su `(rule_id, trigger_key)` → rollback, None (scatto già lavorato); ordine rifiutato dai controlli di rischio (tetto di giornata, leva in giornata, potere d'acquisto del portafoglio o del broker, kill switch, proprietario…) → `AutoTrade.status="rejected"`, `error` = codice e messaggio, nessun invio;
  6. dopo il commit: `wake_broker_sync(redis, entry_order.id)`, `runner.sent(now)`; `ms_trigger_to_send = (sent_at - entry.ts)` in ms.
  Le uscite stop/obiettivo sono i figli del bracket: `held` fino all'esecuzione, poi attivi come coppia sintetica a riposo sorvegliata da `OrderProcessor` (Alpaca: plan 47, gruppo OCO tenuto in SMTrading), anche con lo `scalper` fermo.

- [ ] **Step 1: test falliti** (Redis dei test, `FakeQuoteSource` sotto `CacheQuoteSource` per fx, orologio finto, collegamento `alpaca_paper` finto, `BrokerSync` non avviato salvo dove detto):
  - `test_guards_block_entry` (Review Focus 2, parametrico): quota di 2,1 s con `max_age_s=2` → `stale`; forbice 0,11% con `max_spread_pct=0.10` → `spread`; 2026-11-26 (festività) → `closed`; quota `suspect` → `stale`; operazione `open` della regola → `busy` — in ogni caso nessun ordine, nessuna riga `auto_trades`, contatore `autorules:blocked` incrementato col motivo giusto;
  - `test_no_entry_when_symbol_polled` (Review Focus 2: `md:stream_status` con il simbolo in `polling` → `no_stream`; stato assente → `no_stream`);
  - `test_no_entry_when_brokersync_silent` (Review Focus 2: `brokersync:last_pass` di 11 s fa o assente → `broker_down`);
  - `test_entry_places_bracket` (quota ok → un ordine `market` `source="system"`, `day_trade=True` per AAPL e `False` per BTC-USD, `auto_rule_id` impostato, due figli `held` `take_profit`/`stop_loss` ai prezzi di `levels(ask)`, una riga `AutoTrade(status="sent")` con `expected_price == ask`, un messaggio su `brokersync:wake` col suo id dopo il commit);
  - `test_same_trigger_twice_one_order` (Review Focus 1: `enter` due volte con lo stesso `Entry` → un ordine, una riga, il secondo restituisce None);
  - `test_restart_after_commit_no_second_order` (Review Focus 1: nuovo `Executor` e nuovo `RuleRunner` dopo un commit riuscito ma prima di `runner.sent` → stessa quota e stesso `Entry` → nessun secondo ordine (vincolo unico); quota successiva → `busy`);
  - `test_risk_rejection_recorded` (kill switch acceso → `AutoTrade.status == "rejected"`, `error` contiene il codice, nessun messaggio di sveglia; tetto proprio a zero con `max_day_trades_5d=0` → `DAY_TRADE_LIMIT` registrato; `buying_power` fresco sotto l'importo della regola → `INSUFFICIENT_BUYING_POWER` con `broker_buying_power` registrato; portafoglio con `leverage=2` su collegamento reale (fixture live) e importo oltre il patrimonio → `day_trade_leverage` registrato: le entrate automatiche passano dal passo 10 come `day_trade=True`);
  - `test_limit_entry_price_rounded_up` (`entry="limit"`, ask 100,013, offset 0,05, tick 0,01 → limite 100,07);
  - `test_trigger_to_send_under_one_second` (con `BrokerSync` vero su fake adapter: dallo scatto alla chiamata `submit` del fake < 1 s; `ms_trigger_to_send` scritto).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_scalper_entry.py tests/integration/test_synthetic_orders.py tests/integration/test_order_service.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): pre-send guards and bracket entries through the existing order path`.

---

### Task 22: Esecuzioni, uscite a tempo e di fine seduta, misure, riepilogo serale

**Files:**
- Create: `autorules/tracker.py`, `autorules/summary.py`
- Modify: `autorules/executor.py` (`exit`, `cancel_stale_entry`), `autorules/service.py`
- Test: `tests/integration/test_scalper_tracker.py`

**Interfaces — Consumes:** `AutoTrade`, `Order` (`auto_rule_id`, figli `take_profit`/`stop_loss`), `Fill` (`fee_amount`, `fee_currency`), `close_position(client_order_id=)` (Task 10), `cancel_order`, `wake_broker_sync`, `CacheQuoteSource.get_fx_rate`, `run_job_once`, `enqueue`, `RuleRunner.entered/exited/canceled`.
**Produces:**
- `class Tracker: __init__(self, sessionmaker, redis, qs, *, clock)`; `TRACK_EVERY_S = 0.5`; `async def step(self, runners: dict[UUID, RuleRunner]) -> None` — una query sulle `AutoTrade` `sent`/`open` con i loro ordini; per ognuna:
  - entrata eseguita → `status="open"`, `filled_at` = `raw["filled_at"]` del broker se c'è (`fill_ts_source="broker"`), altrimenti il `created_at` del primo `Fill` (`"local"`); `fill_price`, `qty`, `broker_sent_at` dall'ordine; `entry_slippage_bps = (fill - expected) / expected × 10000`; `ms_send_to_broker`, `ms_broker_to_fill`; `runner.entered(filled_at, fill_price)`;
  - entrata `limit` non eseguita dopo `entry_timeout_s` → `cancel_order(..., by="system")` + sveglia; annullata/rifiutata/scaduta senza esecuzioni → `status="canceled"` (o `rejected`), `runner.canceled()`;
  - figlio `take_profit`/`stop_loss` eseguito → `exit_reason="target"`/`"stop"`, `exit_expected` = suo prezzo; uscita della regola eseguita → motivo già scritto; ordine di chiusura della rete del worker (`client_order_id` `smt-dayclose-…`) → `"day_close"`; altra chiusura reduce-only → `"manual"`. Poi `exit_filled_at`, `exit_price`, `exit_slippage_bps`, `fees` = somma dei `Fill.fee_amount` di entrata e uscita (commissioni vere del broker), `net_result` = `(exit - fill) × qty - fees` convertito nella valuta del portafoglio con `qs.get_fx_rate(valuta dello strumento, base_currency)` (il cambio che usa il portafoglio), `net_currency = base_currency`, `status="closed"`, `runner.exited(exit_filled_at)`.
  - Al riavvio (runner nuovo) lo stato del runner si ricostruisce dalle righe aperte (`entered` con i dati salvati), così uscite a tempo e di fine seduta continuano.
- `Executor.exit(self, rule, runner, ex: Exit, trade: AutoTrade) -> None` — `exit_trigger_at`, `exit_reason` (`time`/`session`), `exit_expected` = bid; `close_position(session, qs, portfolio_id, symbol, qty=trade.qty, source="system", agent_id=None, client_order_id=f"smt-autox-{trade.id}")` (market, reduce-only, annulla i figli del bracket per R16), commit, `wake_broker_sync`. Una vendita reduce-only non è mai rifiutata dal passo 10 (tetto di giornata, leva in giornata, potere d'acquisto del broker) né dal proprietario (passo 11). Errore → log e nuovo tentativo al giro dopo (idempotente per `client_order_id`).
- Flusso perso o broker fermo: le guardie del Task 21 bloccano solo le entrate; `Exit` a tempo/fine seduta parte comunque (l'ordine resta `to_send` finché BrokerSync riprende); stop e obiettivo restano a riposo nel motore delle uscite esistente.
- `summary.py`: `async def evening_summary_text(session, redis, portfolio: Portfolio, day: date) -> str | None` — «Riepilogo regole automatiche · {nome} · {gg/mm}: {n} operazioni ({vinte} in guadagno, {perse} in perdita), risultato netto {x} {valuta} dopo {f} di commissioni; tempi mediani scatto→invio {a} ms, invio→broker {b} ms, broker→esecuzione {c} ms; scarto medio d'entrata {d} punti base; scatti bloccati {k} ({stale} prezzo vecchio, {spread} forbice larga, {no_stream} senza flusso, {broker_down} broker fermo, {closed} mercato chiuso, {busy} già in posizione); freni scattati {m}. Prezzi gratuiti: volume e scambi solo IEX per le azioni.» (numeri all'italiana); None senza operazioni e senza blocchi.
- Nel `Scalper`: `Tracker.step` ogni `TRACK_EVERY_S`; riepilogo per portafoglio con regole a `max(chiusura NYSE + 10 min, 22:30 nel fuso dell'utente)` nei giorni di seduta, alle 22:30 negli altri, una volta con `run_job_once("autorules_summary", f"{pid}:{data}")`.

- [ ] **Step 1: test falliti** (fake adapter Alpaca, `OrderProcessor` e `BrokerSync` veri dove detto):
  - `test_fill_measured` (entrata inviata a t0+0,10 s, al broker t0+0,25 s, `filled_at` del broker t0+0,60 s, prezzo 100,02 con atteso 100,00 → `ms_trigger_to_send == 100`, `ms_send_to_broker == 150`, `ms_broker_to_fill == 350`, `entry_slippage_bps == Decimal("2")`, `fill_ts_source == "broker"`);
  - `test_target_exit_closes_trade` (quota che tocca l'obiettivo → il figlio `take_profit` eseguito dal motore esistente → `exit_reason == "target"`, `fees` = somma dei due `Fill.fee_amount`, `net_result` in EUR con cambio 0,9 su un portafoglio `base_currency="EUR"`);
  - `test_time_exit_sent_by_scalper` (`max_hold_s=60` → a filled_at+60 s un ordine `market` reduce-only `client_order_id="smt-autox-{id}"`, figli annullati, `exit_reason == "time"`);
  - `test_session_exit_before_worker_net` (azioni, `day_close_minutes=10` → ordine della regola alle 20:48Z; alla 20:50Z la rete del worker non trova quantità di giornata);
  - `test_time_exit_never_refused_by_day_trade_checks` (Review Focus 4: `max_day_trades_5d=0` e `buying_power "0"` fresco → l'uscita a tempo è accettata);
  - `test_exits_survive_scalper_stop` (Review Focus 2: entrata eseguita, `Scalper` fermato → una quota sotto lo stop fa eseguire il figlio `stop_loss` da `OrderProcessor` + `BrokerSync`; al riavvio lo `scalper` registra `exit_reason == "stop"`);
  - `test_restart_rebuilds_open_trade` (riavvio con un'operazione `open` → l'uscita a tempo parte all'ora giusta, non prima e non due volte);
  - `test_stale_limit_entry_canceled` (`entry="limit"`, non eseguita dopo 5 s → annullo richiesto, `status == "canceled"`, il runner torna `flat`);
  - `test_day_close_reason` (chiusura dalla rete del worker → `exit_reason == "day_close"`);
  - `test_evening_summary_once` (due operazioni e tre blocchi → una riga outbox `system` con «2 operazioni», «3» bloccati e «solo IEX»; secondo giro → nessuna).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_scalper_tracker.py tests/integration/test_scalper_entry.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): fills, time and session exits, measurements and the evening summary`.

---

### Task 23: Freni per regola e per portafoglio

**Files:**
- Create: `autorules/brakes.py`
- Modify: `autorules/service.py`, `autorules/executor.py` (controllo prima delle guardie)
- Test: `tests/integration/test_scalper_brakes.py`

**Interfaces — Consumes:** `Brakes` (Task 15), `AutoTrade`, `brake`, `active_rules` (Task 16), `corporate.snapshots.ensure_day_open` (patrimonio d'apertura, valuta del portafoglio), `enqueue`.
**Produces:**
- `class BrakeChecker: __init__(self, sessionmaker, qs, *, clock)`; `async def blocked(self, rule: ActiveRule, now) -> str | None` (prima di ogni entrata: `max_trades_hour`/`max_trades_day` raggiunti per la regola o per il portafoglio, `max_open_positions` del portafoglio raggiunto → motivo `braked` contato, nessun cambio di stato: sono tetti, non freni); `async def after_close(self, trade: AutoTrade, now) -> None` (dopo ogni operazione chiusa) — freni che **fermano**:
  - regola: perdite di fila (`net_result < 0` consecutive, ultime righe `closed` della regola nel giorno) `>= max_consecutive_losses`; perdita del giorno della regola `>= max_loss_day` (valuta del portafoglio) o `>= max_loss_day_pct` % del patrimonio d'apertura del giorno (`ensure_day_open(...).equity_eur` convertito nella valuta del portafoglio con lo stesso cambio) → `brake(rule, reason)`;
  - portafoglio (`auto_brakes` validati con `validate_brakes(level="portfolio")`): le stesse regole su tutte le operazioni del portafoglio nel giorno → `brake` di **tutte** le regole attive del portafoglio;
  - «giorno» = giorno locale del portafoglio (come `next_local_midnight`);
  - ogni freno scattato: un `enqueue(source="system", category="system", level="warning")` «Regola automatica fermata dai freni · {portafoglio} · {simbolo} ({modello} v{versione}): {motivo in chiaro}. Riattivala a mano dal portale quando vuoi.»; una sola volta per (regola, giorno, motivo) con `run_job_once("autorules_brake", ...)`.
- Il kill switch esistente resta il freno globale (controlli di rischio dell'engine, Task 21). Un freno non chiude le posizioni aperte: restano con stop, obiettivo e uscite a tempo/fine seduta.

- [ ] **Step 1: test falliti:**
  - `test_consecutive_losses_brake` (Review Focus 5: `max_consecutive_losses=3`, tre operazioni chiuse in perdita → regola `braked`, `status_reason` «3 perdite di fila», una riga outbox; la quota successiva che farebbe scattare → nessun ordine; una vincita in mezzo azzera il conto);
  - `test_daily_loss_in_portfolio_currency` (Review Focus 5: portafoglio EUR, cambio USD→EUR 0,9, `max_loss_day=Decimal("90")`, perdite di 60 $ e 41 $ → 90,9 € ≥ 90 → `braked`; con 60 $ e 39 $ (89,1 €) no);
  - `test_daily_loss_pct` (patrimonio d'apertura 30.000 €, `max_loss_day_pct=1` → freno a 300 €);
  - `test_portfolio_brake_stops_all_rules` (Review Focus 5: due regole, freno di portafoglio sulle perdite di fila sommate → entrambe `braked`, un avviso per regola);
  - `test_trade_caps_block_without_braking` (`max_trades_hour=2`: terza entrata nell'ora bloccata con motivo `braked` contato, regola ancora `active`; l'ora dopo riprende);
  - `test_max_open_positions_portfolio` (`max_open_positions=1` di portafoglio, due regole: la seconda non entra mentre la prima è aperta);
  - `test_brake_keeps_exits` (freno scattato con un'operazione aperta → figli del bracket ancora attivi, uscita a tempo ancora inviata);
  - `test_resume_after_brake_is_manual` (nessun ritorno automatico ad `active` nel giorno dopo; `resume` lo riporta).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_scalper_brakes.py tests/integration/test_scalper_tracker.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): per-rule and per-portfolio brakes with Telegram notice`.

---

### Task 24: Prova sullo storico al minuto e parità con il motore

**Files:**
- Create: `autorules/replay.py`
- Test: `tests/unit/test_autorules_replay.py`, `tests/integration/test_scalper_parity.py`

**Interfaces — Consumes:** `RuleRunner`, `validate_params` (Task 15), `fees.calculator.FeeRuleData, compute_trade_fee`, `marketdata.types.Bar`; per la parità il `Scalper`/`Executor`/`Tracker` dei Task 20-22 con fake broker.
**Produces:**
- `def price_path(bar: Bar) -> list[tuple[datetime, Decimal]]` — quattro punti a `ts`, `ts+15s`, `ts+30s`, `ts+45s`: `O, L, H, C` se `close >= open`, altrimenti `O, H, L, C` (limite dichiarato: al minuto non si vedono i movimenti dentro il minuto).
- `@dataclass(frozen=True) class ReplayTrade: entry_ts; entry_price; exit_ts; exit_price; exit_reason; qty; fees: Decimal; net: Decimal`; `@dataclass(frozen=True) class ReplayResult: trades: list[ReplayTrade]; net: Decimal; fees: Decimal; wins: int; losses: int; max_drawdown: Decimal; notes: list[str]` (sempre la nota «al minuto non si vedono i movimenti dentro il minuto; volume solo IEX per le azioni con il flusso gratuito»).
- `def replay(bars: list[Bar], *, model: str, params: dict, market: str, tick_size: Decimal, qty: Decimal | None, notional: Decimal | None, fee_rule: FeeRuleData, spread_pct: Decimal, delay_s: int, day_close_minutes: int) -> ReplayResult` — per ogni barra: i punti di `price_path` passano a `runner.on_price` con `bid = p × (1 - spread_pct/200)`, `ask = p × (1 + spread_pct/200)`; poi la barra passa a `runner.on_bar` (stesso ordine del vivo: la barra del minuto arriva dopo i suoi prezzi). Entrata: eseguita al primo punto con `ts >= scatto + delay_s` al suo `ask`; livelli `runner.levels(ask allo scatto)`; uscite: stop toccato (`p <= stop`) → al `min(stop, bid)`; obiettivo (`p >= target`) → al `target`; stop e obiettivo nello stesso punto → stop (prudente); `Exit` del runner → al `bid` del primo punto dopo `delay_s`. Commissioni `compute_trade_fee(fee_rule, nozionale)` su entrata e uscita.
- Endpoint e limiti nel Task 25 (sincrono, admin): al massimo 30 giorni e 5 simboli.

- [ ] **Step 1: test falliti:**
  - unit `test_autorules_replay.py`: `test_price_path_orders` (barra in salita → O,L,H,C; in discesa → O,H,L,C), `test_breakout_trade_with_fees_and_spread` (serie costruita: 20 barre a 100, poi una barra 100→101,2 → un'operazione, entrata all'`ask` del punto dopo il ritardo, uscita all'obiettivo 0,5%, `fees` = due volte `compute_trade_fee` (regola Alpaca cripto 0,25%), `net` scritto nel test a mano), `test_stop_wins_tie`, `test_time_exit` (`max_hold_s=120`), `test_delay_moves_entry` (`delay_s=0` → entrata al punto dello scatto; `delay_s=20` → al punto successivo), `test_no_entry_in_session_windows`, `test_notes_declare_minute_limit`;
  - integration `test_scalper_parity.py`: `test_engine_matches_replay` — stessa serie di 120 barre da 1 minuto per AAPL con due scatti e uscite (un obiettivo, una a tempo): (a) `replay(..., spread_pct=0, delay_s=0, fee_rule=zero)`; (b) lo `Scalper` vero (Task 20-22) con fake broker che esegue al prezzo atteso, alimentato con le stesse barre (`bars1m`) e quote costruite da `price_path` (`bid = ask = p`), `OrderProcessor` vero per stop e obiettivo → stessa lista di `(entry_ts, entry_price, exit_reason, exit_price)` arrotondata al tick, stesso numero di operazioni; le differenze ammesse sono solo i tempi d'esecuzione in millisecondi.
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/unit/test_autorules_replay.py tests/integration/test_scalper_parity.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): one-minute replay with fees, spread and delay, and engine parity`.

---

### Task 25: API admin delle regole automatiche

**Files:**
- Create: `admin/routes_auto_rules.py`
- Modify: `admin/schemas.py`, `admin/routes_portfolios.py` (`auto_brakes` nel PATCH e nei campi), `api/main.py` (router), `cli.py` (pulizia di prova)
- Test: `tests/integration/test_admin_auto_rules.py`

**Interfaces — Consumes:** `rules.py` (Task 16), `model_catalog`, `validate_brakes` (Task 15), `stream_state` (Task 20), `AutoTrade`, `replay` (Task 24), `MarketDataRouter.get_bars`, contatori `autorules:blocked`.
**Produces (solo admin, `require_admin`, ogni scrittura con `write_audit(method="admin:auto_rules.<azione>")`, mai MCP):**
- `GET /api/auto-rules/models` → `model_catalog()`.
- `GET /api/auto-rules?portfolio_id=` → `[{id, portfolio_id, portfolio_name, symbol, model, status, status_it, status_reason, version, params, qty, notional, brakes, stream: "stream"|"polling"|"none", stream_it: "con flusso"|"in polling"|"senza flusso", open_trade: bool, today: {trades, net, net_currency}}]`.
- `POST /api/auto-rules` (crea in bozza), `PATCH /api/auto-rules/{id}` (nuova versione), `POST /api/auto-rules/{id}/activate|pause|resume` → DTO della regola; errori del servizio come `SMTError` (testi italiani del Task 16).
- `GET /api/auto-rules/{id}/trades?limit=` (default 100, max 500) → righe `auto_trades` (tutti i campi, numeri come stringhe).
- `GET /api/auto-rules/results?portfolio_id=&from=&to=` → per regola e per giorno `{rule_id, version, day, trades, wins, losses, net, fees, median_ms_trigger_to_send, median_ms_send_to_broker, median_ms_broker_to_fill, avg_entry_slippage_bps, blocked: {motivo: n}}`.
- `POST /api/auto-rules/backtest` `{symbol, model, params, qty|notional, from, to, spread_pct (default 0.05), delay_s (default 1), fee_rule: "broker"|"zero"}` → `ReplayResult`; al massimo 30 giorni, `RISK_LIMIT_EXCEEDED` oltre; barre da `router.get_bars(1m)`; commissioni del broker del portafoglio o del collegamento indicato (regole fee esistenti).
- `PATCH /api/portfolios/{id}` con `auto_brakes` (validati, `level="portfolio"`); `PORTFOLIO_FIELDS` + `auto_brakes`.
- `DELETE /api/portfolios/{id}/auto-rules` → `{rules, trades}` cancellati (regole, versioni, operazioni; `orders.auto_rule_id` va a null da sé), solo se il portafoglio non ha righe `agent_portfolios`, nessuna regola `active` e nessuna operazione `sent`/`open`; poi `untrack` dei simboli e cancellazione delle chiavi Redis `autorules:blocked:{rule}:*` e `bars1m:{symbol}` dei simboli non più usati da regole; audit `admin:auto_rules.purge`. CLI gemello: `python -m smtrading.cli autorules purge --portfolio <id>`.

- [ ] **Step 1: test falliti:** `test_auto_rules_admin_only` (viewer → 403 su tutte le rotte di scrittura e sul backtest), `test_create_update_activate_flow` (bozza → versione 2 → attiva → DTO `status_it == "attiva"`; audit per ogni passo), `test_activate_errors_in_italian` (portafoglio spento → 4xx col testo del Task 16), `test_stream_state_in_dto` (stato pubblicato con il simbolo in polling → `stream_it == "in polling"`; assente → «senza flusso»), `test_results_per_rule_and_day` (righe fittizie su due giorni → due voci con mediane e scarti attesi), `test_backtest_endpoint_limits` (31 giorni → `RISK_LIMIT_EXCEEDED`; 2 giorni con respx su `1Min` → `ReplayResult` con `notes`), `test_portfolio_brakes_patch` (valore fuori limite → `INVALID_ORDER`; valido → salvato e in audit), `test_purge_refuses_and_deletes` (con regola attiva → rifiuto; con agente sul portafoglio → rifiuto; altrimenti regole e operazioni cancellate, `md:tracked:auto_rules` vuoto, chiavi `autorules:blocked:*` del portafoglio sparite), `test_no_secret_in_dto` (nessun `key_id`/ID conto in nessuna risposta), `test_mcp_unchanged_after_routes` (`test_tool_catalog_unchanged_by_intraday` verde).
- [ ] **Step 2:** eseguire → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `uv run pytest -q -p no:cacheprovider tests/integration/test_admin_auto_rules.py tests/integration/test_admin_portfolios.py tests/unit/test_mcp_catalog_frozen.py` → PASS.
- [ ] **Step 5: commit** `feat(autorules): admin API for rules, trades, results, backtest and cleanup`.

---

### Task 26: Portale, pagina «Regole automatiche»

**Files:**
- Create: `web/src/api/hooks/autoRules.ts`, `web/src/pages/AutoRules.tsx`, `web/src/pages/AutoRules.test.tsx`
- Modify: `web/src/api/types.ts`, `web/src/App.tsx` (rotta `/auto-rules`), la navigazione admin, `web/src/pages/PortfolioDetailSettings.tsx` (freni di portafoglio)

**Interfaces — Consumes:** il contratto del Task 25.
**Produces:** `useAutoRuleModels()`, `useAutoRules(portfolioId?)`, `useCreateAutoRule()`, `useUpdateAutoRule(id)`, `useAutoRuleAction(id, "activate"|"pause"|"resume")`, `useAutoRuleTrades(id)`, `useAutoRuleResults(params)`, `useAutoRuleBacktest()`; tipi `AutoRule`, `AutoTrade`, `AutoRuleResult`, `ReplayResult`, `ModelSpec`.

- Pagina «Regole automatiche» (voce di menu solo per admin, `AdminOnly`): elenco con portafoglio, simbolo, modello e versione, stato in chiaro («bozza», «attiva», «in pausa», «fermata dai freni» con il motivo), segno «senza flusso»/«in polling», risultato di oggi; pulsanti Attiva / Metti in pausa / Riattiva; modulo crea/modifica con i parametri del modello letti da `/models` (campi numerici con min/max dai limiti, testo d'aiuto per ognuno), quantità (pezzi **o** importo), freni della regola; schede «Risultati» (per regola e per giorno: operazioni, vinte/perse, netto, commissioni, tempi mediani in ms, scarto medio, scatti bloccati per motivo) e «Operazioni» (orari, prezzi atteso/eseguito, motivo d'uscita in chiaro: «stop», «obiettivo», «a tempo», «fine seduta», «rete di fine seduta», «manuale»); pannello «Prova sullo storico» (date, forbice, ritardo, commissioni) con il risultato e la nota sul limite del minuto. Testo in testa: «Prove: le regole lavorano solo sui portafogli con l'interruttore acceso. Il kill switch ferma tutto.»
- Impostazioni del portafoglio: sezione «Freni delle regole automatiche» (visibile con `auto_rules_enabled`).

- [ ] **Step 1: test falliti (Vitest + msw):** `AutoRules` non mostra la voce di menu a un viewer; il modulo invia `POST` con i default dei parametri e rifiuta in pagina un valore fuori dai limiti del catalogo; «Attiva» chiama `/activate` e un errore 400 mostra il testo del server; una regola con `stream: "none"` mostra «senza flusso»; la scheda Operazioni mostra «a tempo» e i millisecondi; il pannello di prova mostra la nota «al minuto non si vedono i movimenti dentro il minuto».
- [ ] **Step 2:** `cd web && npx vitest run src/pages/AutoRules.test.tsx src/pages/PortfolioDetailSettings.test.tsx` → FAIL.
- [ ] **Step 3:** implementare.
- [ ] **Step 4:** `cd web && npx vitest run && npm run lint && npm run build` → PASS.
- [ ] **Step 5: commit** `feat(web): automatic rules page (rules, brakes, results, trades, replay)`.

---

### Task 27: Prova di carico (30 simboli)

**Files:**
- Create: `tests/integration/test_autorules_load.py` (nessun codice di prodotto, salvo un difetto trovato)

**Interfaces — Consumes:** `Scalper` (Task 20-23), `OrderProcessor`, `write_quote`, `publish_minute_bar`.

- [ ] **Step 1: test** (Redis dei test, orologio vero, durata ≤ 30 s):
  - `test_scalper_keeps_up_with_30_symbols`: 30 regole attive (30 simboli, modelli misti), 20 quote/s per simbolo per 20 s con `write_quote` (stesso percorso del flusso) e una barra al minuto simulata ogni 5 s → nessuna quota persa per più di un lotto (ogni simbolo valutato almeno 15 volte/s in media), tempo di valutazione per quota p99 < 5 ms (misurato attorno a `on_event`), memoria dei runner limitata (`MinuteSeries.maxlen`);
  - `test_existing_exits_not_delayed`: uno stop a riposo di un portafoglio **senza** regole su un simbolo fuori dalle 30; misura del tempo dalla quota che lo attraversa al suo stato `triggered` con lo `scalper` fermo e poi sotto il carico sopra → la differenza delle mediane < 50 ms e il p95 sotto carico < `min_eval_interval_s` dell'`OrderProcessor` + 250 ms;
  - `test_stream_manager_unaffected`: con le 30 regole il gestore del flusso (fake provider) pubblica tutte le quote dei simboli della campagna con lo stesso ritardo medio (± 20%) misurato senza regole.
- [ ] **Step 2:** eseguire; se un test fallisce per un difetto, correggere nel modulo indicato e annotarlo nel report (il carico non deve diventare un limite più largo nel test).
- [ ] **Step 3:** `uv run pytest -q -p no:cacheprovider tests/integration/test_autorules_load.py` → PASS.
- [ ] **Step 4: commit** `test(autorules): load test with 30 symbols, existing exits not delayed`.

---

### Task 28: Verifica finale e messa in linea spenta

**Files:** nessuno nuovo.

- [ ] **Step 1:** suite completa: `uv run pytest -q -p no:cacheprovider` e `cd web && npx vitest run && npm run lint && npm run build` → tutto verde; `test_tool_catalog_unchanged_by_intraday` verde.
- [ ] **Step 2:** grep di guardia sul diff del ramo: nessun nome di agente o codice di portafoglio nei file nuovi (`grep -rn "Portfolio Manager\|Risk Manager" backend/src/smtrading/intraday backend/src/smtrading/autorules` vuoto, prosa e docstring comprese); nessuna modifica in `docs/paperclip-agents/` e in `backend/src/smtrading/mcp/` (`git diff main --stat -- docs/paperclip-agents backend/src/smtrading/mcp` vuoto); nessun simbolo fisso in `autorules/` fuori dai test; nessun resto della regola PDT (`grep -rnI "pdt_applies\|PDT_MAX\|PDT_EQUITY_MIN\|link_budget\|warn_day_trade_sell\|daytrade_count\|pattern_day_trader\|25000\|25\.000" backend/src/smtrading web/src` vuoto; `pdt_state` resta solo come nome di colonna).
- [ ] **Step 3:** messa in linea **solo con gli agenti Paperclip fermi** (verifica sulla pagina Paperclip del portale: nessun ciclo in corso), prima annotare `python -m smtrading.marketdata.cli status` (simboli in stream e in polling); backup; migrazioni `0046` e `0047` applicate; `docker compose up -d --build api worker scalper web`; poi:
  - `SELECT count(*) FROM portfolios WHERE intraday_enabled OR auto_rules_enabled` → `0`; `SELECT count(*) FROM auto_rules` → `0`;
  - servizio `scalper` in esecuzione e fermo: battito «scalper» in «Salute del sistema» con 0 regole attive, nei log nessuna richiesta di barre e nessun abbonamento; `SMEMBERS md:tracked:auto_rules` vuoto;
  - `python -m smtrading.marketdata.cli status` → stessi simboli in stream e in polling di prima della messa in linea, nessuna chiave `bars` negli stream;
  - job «intraday» presente in «Salute del sistema» e fermo a una query per giro; BrokerSync con la stessa cadenza (battito `brokersync:last_pass` ogni ~2 s);
  - impronta del catalogo MCP uguale a `FROZEN_FINGERPRINT` (nessun reset delle sessioni agenti da `paperclip.sessions`).
- [ ] **Step 4:** messaggio Telegram al gruppo con l'avanzamento (barra %), in chiaro: «Lavoro in giornata nei due modi (sveglia dell'agente e regole automatiche) costruito e in linea spento; il servizio delle regole automatiche è acceso ma senza regole; la prova reale aspetta le chiavi del nuovo conto di prova».

---

### Task 29: Manuale della prova reale dei due modi (nessun codice)

**Files:**
- Create: `docs/readiness/2026-12-intraday-prova-alpaca.md`
- Modify: `docs/readiness/2026-12-campagna-prontezza.md` (sezione 3, una riga nella tabella)

**Prerequisito:** chiavi di **un nuovo** conto di prova Alpaca con saldo **30.000 $** (conto con margine; la regola PDT non esiste più dal 4/6/2026, il rifiuto di giornata si prova con `max_day_trades_5d` e quello d'importo con il potere d'acquisto), aperto da Luca/Matteo, con un collegamento proprio. **Finché le chiavi non ci sono il task resta aperto e si salta** (nessun altro task dipende da esso); appena arrivano si esegue dal passo 2.

- [ ] **Step 1: scrivere il manuale** (italiano, passi numerati, ognuno con il controllo atteso):
  1. Prerequisiti: chiavi ricevute; giorno di seduta piena, non mezza giornata; agenti tranquilli; nessuna messa in linea in corso; stato del flusso annotato (`python -m smtrading.marketdata.cli status`).
  2. Collegamento broker **nuovo** `alpaca_paper` dal portale con le chiavi del nuovo conto (mai il collegamento del Portafoglio Alpaca della campagna); test in sola lettura; `GET /api/intraday/accounts` → stato del conto letto (`buying_power`, `multiplier` > 1, `margin: true`, `trading_blocked`/`account_blocked` false, `fresh: true` dopo l'accensione di un interruttore).
  3. Due portafogli di prova nascosti sullo stesso collegamento: `in_totals=false`, **nessuna** riga `agent_portfolios`, **`leverage=2`** su entrambi (punto 6: «Fino a 2, solo in prova»; un tentativo con 2,5 è rifiutato dal portale). **Prova A**: `intraday_enabled=true`, `intraday_timeframes=["15m"]`, nessun agente da svegliare (gli scatti vanno su Telegram «system»), `max_day_trades_5d=1`. **Prova B**: `auto_rules_enabled=true` (il portale accetta: conto con margine, non bloccato), `max_day_trades_5d=2`, freni di portafoglio di default.
  4. Modo A: strategia 15m (RSI o incrocio) su 1-2 titoli liquidi già seguiti dalla campagna, approvata per la sola Prova A con `python -m smtrading.cli intraday drill-strategy ...`; per alcune sedute scatti registrati nella pagina Segnali e messaggi Telegram alle chiusure di barra giuste; nessun messaggio a mercato chiuso.
  5. Modo B: su Prova B 1-2 regole su titoli liquidi **diversi** da quelli del modo A (`breakout` e `vwap_pullback`, importi piccoli) e 1-2 regole su cripto liquide (`mean_reversion`); prima, prova sullo storico di ognuna dalla pagina «Regole automatiche» (ultimi 5 giorni) annotata. Attivazione: la pagina mostra «con flusso»; `python -m smtrading.marketdata.cli status` → i simboli della campagna con ordini e posizioni ancora tutti nello stream, barre solo per i simboli delle regole.
  6. Profondità dello storico gratuito: `python -m smtrading.marketdata.cli bars <SIMBOLO> 15m --days 60` e `1m --days 5`; annotare la prima barra e i buchi.
  7. Misure del modo B per alcune sedute: tempi scatto→invio (obiettivo < 1 s), invio→broker, broker→esecuzione, scarto atteso→eseguito, commissioni vere, risultato netto; riepilogo serale su Telegram; confronto con la prova sullo storico degli stessi giorni (scarto annotato).
  8. Uscite del modo B: un'uscita a tempo (`max_hold_s=60` su una regola) e un'uscita di fine seduta (posizione aperta apposta vicino alla fine) mandate dallo `scalper`; uno stop o un obiettivo eseguito dalla coppia sintetica.
  9. Freni: su una regola `max_trades_hour=1` → seconda entrata dell'ora bloccata; `max_consecutive_losses=1` → dopo una perdita «fermata dai freni» con un avviso Telegram; riattivazione manuale.
  10. Proprietario: con una regola attiva un acquisto manuale sul suo simbolo dal portale è rifiutato («gestito da una regola automatica»); la chiusura manuale della posizione è accettata.
  11. Tetto, leva e potere d'acquisto: un'entrata e un'uscita manuale «di giornata» su Prova A (1 operazione) → la seconda entrata di giornata su Prova A è rifiutata con «Tetto operazioni di giornata del portafoglio raggiunto …» (`max_day_trades_5d=1`), una vendita resta sempre possibile, Prova B non è toccata dal tetto di Prova A (nessun budget per collegamento); un acquisto «di giornata» con importo oltre il `buying_power` mostrato nel riquadro (o oltre patrimonio × 2) è rifiutato con «oltre il potere d'acquisto del conto» (o «oltre la leva ammessa»); il riquadro Posizioni mostra potere d'acquisto, margine, ora di lettura e «usate 1 su 1» per Prova A.
  12. Rete di fine seduta: lasciare apposta una posizione di giornata aperta su Prova A → ordine di sistema `day_close_minutes` prima della fine, messaggio Telegram, posizione chiusa prima della fine; su Prova B la rete non trova nulla (lo `scalper` è uscito 2 minuti prima).
  13. Confronto dei prezzi IEX con quelli consolidati (ultimo prezzo del portale vs grafico pubblico) e del volume IEX, annotato per la pagella.
  14. Nessun disturbo alla campagna: nessun compito Paperclip nuovo, nessun cambio nei portafogli della campagna, catalogo MCP invariato, stato del flusso della campagna uguale al passo 1, uscite della campagna nei tempi di sempre.
  15. Pulizia completa: regole in pausa; posizioni chiuse e **conto vuoto** (`GET /v2/account` e posizioni Alpaca: nessuna posizione, nessun ordine aperto, residui di cripto chiusi); `DELETE /api/portfolios/{id}/intraday-triggers` su Prova A; `DELETE /api/portfolios/{id}/auto-rules` su Prova B (regole, versioni, operazioni, chiavi Redis `autorules:*` e `bars1m:*` dei simboli di prova); strategia della prova **cancellata** con `intraday drill-revoke --purge` (decisioni del portafoglio di prova); righe di audit dei portafogli di prova eliminate (come per le prove reali di novembre: nessuna traccia per gli agenti); `md:tracked:auto_rules` vuoto; interruttori spenti, portafogli archiviati, collegamento disattivato; `SELECT count(*) FROM portfolios WHERE (intraday_enabled OR auto_rules_enabled) AND status <> 'archived'` → 0; esito nel documento di prontezza.
- [ ] **Step 2:** aggiungere alla tabella della sezione 3 di `2026-12-campagna-prontezza.md` la riga: `| quando arrivano le chiavi del nuovo conto di prova Alpaca da 30.000 $ (seduta piena) | Prova reale del lavoro in giornata nei due modi (due portafogli di prova nascosti sullo stesso collegamento nuovo: sveglia su strategia 15m; regole automatiche su 1-2 titoli e 1-2 cripto) | scatti e messaggi alle chiusure di barra; tempi scatto→esecuzione, scarti, commissioni e risultati delle regole; uscite a tempo e di fine seduta; freni; rifiuto del manuale sul simbolo di una regola; tetto di giornata, leva 2 e potere d'acquisto del broker; rete di fine seduta; nessun disturbo alla campagna; pulizia con conto vuoto |`.
- [ ] **Step 3: commit** `docs(readiness): live drill runbook for both intraday modes`.
- [ ] **Step 4 (con le chiavi):** eseguire il manuale; esito annotato nella riga della sezione 3 («fatta: …») e nella pagella; messaggio Telegram con l'esito.
