# SMTrading — Piano 9: eToro e miglioramenti

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** un terzo broker, eToro, in prova (`etoro_demo`) e reale (`etoro_live`, costruito e **spento**), eToro come fonte di dati di mercato per tutti i portafogli (connettore `etoro`), più i miglioramenti della prima settimana: chiusure di borsa manuali e Londra, ordini manuali completi dal portale, più valute nel reale, «Decisioni di oggi» nel riepilogo con campi strutturati di `log_decision`, lettore Paperclip (costi e allarme isolamento) e pulizia dei nomi.

**Architecture:**
- **Miglioramenti prima, denaro reale eToro per ultimo.** I Task 1-12 non toccano eToro. I Task 13-16 aggiungono eToro in quattro passi: protocollo e demo, poi eToro come fonte di dati di mercato per tutti i portafogli, poi l'integrazione della demo, poi il reale dietro il cancello del piano 8.
- **eToro, host condiviso:** demo e reale usano lo stesso host. La guardia non distingue per host ma **per percorso**:
  - il nucleo `_EtoroProtocol` controlla ogni richiesta contro i prefissi ammessi dalla classe concreta, prima di toccare la rete;
  - i prefissi reali sono scritti solo in `brokers/live/etoro_live.py`, e un test scandisce il repository alla ricerca di quei percorsi.
- **Chiusure manuali:** `market_status` resta sincrono. Legge un registro in memoria del processo (`ClosureBook`), che un compito di API e worker ricarica dal database ogni 60 s; il salvataggio dall'admin lo ricarica subito nell'API.
- **Più portafogli reali su un conto (Task 11-12):** il vincolo «un collegamento reale ↔ un portafoglio» del piano 8 cade. Ogni portafoglio ha una quota del contante del conto; contante e riconciliazione si controllano per collegamento, la sola chiusura colpisce solo i portafogli coinvolti. Nessuna migrazione: non esiste un vincolo UNIQUE su `portfolios.broker_link_id` (verificato), la regola 1:1 è solo nel codice.
- **Etichette delle modalità (Task 10):** solo portale. Le DTO admin ricevono campi nuovi; `mode_fields`, che usano anche gli strumenti MCP, non cambia.
- **eToro come fonte di dati (Task 14):** un provider `etoro` nel registro di `marketdata`, con chiavi proprie nel connettore (non quelle dei collegamenti). Riusa il nucleo HTTP e la cache strumenti del Task 13, ma può chiamare solo `GET` sotto `/api/v1/market-data/`. Nasce spento e in fondo alle priorità.
- **Paperclip:** un job del worker ogni 5 minuti fa solo richieste `GET`, con un client che rifiuta ogni altro metodo. Gli avvisi vanno direttamente in `telegram_outbox` (categoria `system`), mai nel feed attività, che gli agenti leggono.
- Il piano parte da `main` 045f598 più i commit dello spec, sul ramo `feat/plan-09-etoro-improvements`.

**Tech Stack:** come i piani 1-8 (FastAPI, SQLAlchemy async, Alembic, httpx, respx, pyotp, `exchange_calendars`, mcp 2.x, React + TanStack Query + Vitest, Playwright). Nessuna dipendenza nuova.

**Spec:** `docs/superpowers/specs/2026-11-15-smtrading-09-etoro-and-improvements-design.md`. Valgono anche le decisioni del controller in `.superpowers/sdd/2026-11-15-smtrading-09-etoro-improvements/progress.md` e le regole R8.1-R8.13 del piano 8 (`.superpowers/sdd/2026-11-14-smtrading-08-real-trading/progress.md`). Percorsi Python relativi a `backend/src/smtrading/` salvo indicazione.

## Global Constraints

- **Regole dei piani 1-8:**
  - `Decimal`, mai float;
  - `json_safe`;
  - testi utente in italiano;
  - `SMTError` con `ErrorCode`;
  - audit admin con `write_audit(session, agent_id=None, method="admin:…")`;
  - nessun I/O di rete sotto lock o dentro una transazione aperta (R9);
  - niente eseguiti locali su portafogli collegati (R10);
  - ordine dei lock R8.9.
- **TDD** in ogni task: prima i test falliti, poi il codice. Ogni task è rivisto prima del successivo.
- **Git e deploy:**
  - mai `git add -A`: si aggiungono solo i file del task;
  - ogni commit termina con `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
  - deploy solo da un worktree pulito su HEAD;
  - con migrazioni: prima `docker compose run --rm migrate`, poi `docker compose up -d --no-build --no-deps <servizi>`.
- **Migrazioni lineari dopo `0023_user_activity.py`** (main ha `0022_admin_user_roles.py` e `0023_user_activity.py`), nell'ordine dei task:
  - `0024_market_closures.py` (Task 2);
  - `0025_decision_fields.py` (Task 3);
  - `0026_live_fx_ref.py` (Task 7);
  - `0027_paperclip.py` (Task 8);
  - `0028_etoro_connector.py` (Task 14, solo dati: riga del connettore e priorità).
  Ognuna ha upgrade e downgrade; `test_migration_matches_models` resta verde.
- **Nessuna attivazione del reale.**
  - Nessun task crea un collegamento `etoro_live` (o un altro live) nello stack principale, né salva chiavi reali o arma un collegamento.
  - Alla fine: 0 collegamenti `etoro_live` in produzione.
  - Nei test solo chiavi «canarino»: `CANARYAPIKEY0001`, `CANARYUSERKEY-do-not-use`.
- **eToro:**
  - acquisti solo con `transaction: "buy"`, `leverage: 1`, `settlementType: "real"`;
  - vendite solo come chiusure di posizioni;
  - nessun payload contiene mai `sellShort`, una leva diversa da 1 o `settlementType` diverso da `real`, né in demo né in reale.
- **Agenti (`mcp_hide_mode`):**
  - nessuna parola da simulazione negli output MCP;
  - nuovi codici d'errore solo se indistinguibili da un broker reale;
  - le istruzioni agenti cambiano solo con il testo approvato dall'utente (Task 4) e non citano eToro.
- **Segreti:** chiavi eToro (dei collegamenti e del connettore `etoro`) e chiave board di Paperclip
  - sono cifrate (`encrypt_json` con `aad`) e registrate con `register_secret`;
  - non compaiono mai in risposte, log, audit, outbox, `broker_raw`, CSV o MCP;
  - nel portale si vede solo `••••` più le ultime 4 cifre.
- **Test senza rete:**
  - respx con `assert_all_mocked=True`;
  - la fixture `_no_live_network` blocca anche l'host eToro (Task 13), quindi anche il provider dati `etoro` (Task 14).

## Review Focus

1. **Vendita eToro più grande della prima posizione, con chiusura parziale non ammessa** (`allowPartialClosePosition=false`). Atteso:
   - le posizioni intere si chiudono in ordine FIFO;
   - il resto è un'esecuzione parziale normale e l'ordine si chiude `canceled` con `filled_qty` uguale a quanto chiuso;
   - mai `sellShort`, mai un messaggio specifico di eToro verso gli agenti.
   Test in Task 13.
2. **Chiusura manuale salvata mentre il worker gira.** Atteso:
   - l'API la applica subito, il worker entro 60 s;
   - un ordine a mercato su quel mercato resta `to_send` (mercato chiuso) e non parte.
   Test in Task 2.
3. **Provider FX non disponibile su un portafoglio reale con valuta diversa da quella del conto.** Atteso:
   - un acquisto attende e scade dopo `LIVE_WAIT_MAX`;
   - una vendita parte;
   - la riconciliazione segna la liquidità `skipped`, senza sola chiusura e senza avviso di scarto di cambio.
   Test in Task 7.
4. **Paperclip risponde 200 con un elenco di policy vuoto**, per esempio con un id azienda sbagliato o senza permessi. Atteso:
   - al primo avvio la fotografia non viene registrata: stato «sconosciuto», con messaggio;
   - con una fotografia esistente vale come «tutte rimosse»: un solo avviso critico, mai una nuova fotografia automatica.
   Test in Task 8.
5. **Ordine manuale su un portafoglio reale** con un TOTP riusato, o da un admin senza TOTP attivo. Atteso: `FORBIDDEN`, nessun ordine salvato. L'anteprima funziona senza TOTP. Test in Task 5.
6. **Due portafogli reali sullo stesso conto con lo stesso simbolo.** A ha 3 AAPL, B ne ha 2. Atteso:
   - broker con 5 → in regola;
   - broker con 4, confermato al secondo passaggio → sola chiusura per A e B, e per nessun altro portafoglio del collegamento;
   - A non può spendere la quota di B; contante del broker sotto la somma delle quote → sola chiusura per tutti;
   - «Disattiva denaro reale» ferma gli ordini di entrambi; un terzo portafoglio la cui quota supera il contante libero non si collega.
   Test in Task 11.
7. **Connettore dati `etoro` a quota esaurita** (`RateLimit-Remaining: 0` o un 429 con `Retry-After`). Atteso:
   - nessuna attesa dentro il router: la chiamata fallisce subito con un errore redatto e il router usa la fonte successiva;
   - fino al reset nessuna richiesta parte verso eToro (la quota persa non si spreca in tentativi);
   - le chiavi non compaiono in `last_error`, log o errori verso gli agenti.
   Test in Task 14.

---

## File Structure

```
backend/src/smtrading/
  common/fingerprints.py        FINGERPRINT, name_warning (unica lista di parole)
  instruments/market_hours.py   (+ LSE, ClosureBook, previous_session_open)
  instruments/closures.py       MarketClosure, refresh_closures, ClosureRefresher, CRUD
  activity/models.py, service.py (+ outcome/reason/subject)
  telegram/summaries.py         (+ «Decisioni di oggi»)
  admin/routes_orders.py        ordini manuali (anteprima, invio)
  risk/live_checks.py           (+ cambio peggiore, giorno di borsa)
  brokers/live/reconcile.py     (+ scarto di cambio)
  paperclip/{config,client,models,reader}.py   admin/routes_paperclip.py
  portfolios/mode.py            (+ broker_kind, mode_label solo per il portale)
  brokers/live/quotas.py        quote del contante per collegamento (Task 11)
  brokers/etoro_core.py         _EtoroProtocol, ETORO_URL, cache strumenti
  brokers/etoro_demo.py         EtoroDemoAdapter
  brokers/live/etoro_live.py    EtoroLiveAdapter (unico posto con i percorsi reali)
  brokers/live/fake.py          (+ eToro, + dati di mercato eToro)
  marketdata/providers/etoro.py EtoroProvider (quote, bars; solo GET market-data)
  cli.py                        (+ names audit|fix)
backend/migrations/versions/0024_…, 0025_…, 0026_…, 0027_…, 0028_etoro_connector.py
web/src/components/OrderForm.tsx, PaperclipBanner.tsx, PaperclipSection.tsx
web/src/pages/AgentCosts.tsx    web/src/api/hooks/paperclip.ts
web/src/components/LiveLinkPortfolios.tsx
```

| Ondata | Task | Note |
|---|---|---|
| A | 1 → 2 → 3 → 4 | in sequenza (migrazioni 0024, 0025) |
| B | 5 → 6 | 6 solo `web/` |
| C | 7 | migrazione 0026 |
| D | 8 → 9 | migrazione 0027; 9 solo `web/` |
| E | 10 → 11 → 12 | etichette delle modalità; più portafogli reali per conto (backend, poi web); nessuna migrazione |
| F | 13 → 14 → 15 → 16 | eToro: protocollo e demo, fonte di dati (migrazione 0028), demo nel portale, reale per ultimo |
| G | 17 | e2e, documentazione, verifica |

---

### Task 1: Parole vietate in un solo modulo, avviso sui nomi, CLI `names`

**Files:**
- Create: `common/fingerprints.py`, `tests/unit/test_fingerprints.py`, `tests/integration/test_names_cli.py`
- Modify: `tests/integration/test_mcp_no_fingerprints.py` (importa `FINGERPRINT`), `admin/routes_portfolios.py` (POST/PATCH), `admin/routes_agents.py` (create/patch), `admin/routes_risk.py` (profili), `cli.py`; web: `pages/Portfolios.tsx`, `pages/PortfolioDetailSettings.tsx`, `pages/Agents.tsx`, `pages/Risk.tsx` (mostrano l'avviso)

**Interfaces — Produces:**
- `FINGERPRINT: re.Pattern[str]`: il regex attuale del test più `smoke`:
  `r"\bsim\b|simul|paper|demo|\bdry|\bprova\b|\btest|fake|mock|sandbox|virtual|smoke"`, `re.IGNORECASE`.
- `NAME_WARNING = "gli agenti vedranno questo nome"`.
- `def name_warning(name: str) -> str | None`: restituisce `NAME_WARNING` se `FINGERPRINT` trova una corrispondenza in `name`.
- **Risposte di salvataggio** di portafoglio, agente e profilo di rischio (create e patch): campo nuovo `name_warning: str | null`. È solo un avviso: il salvataggio non viene mai bloccato. Il web lo mostra come avviso giallo sotto il campo nome dopo il salvataggio.
- **CLI `python -m smtrading.cli names audit`.** Elenca, una riga per voce, `tipo;id;nome attuale;nome proposto`:
  - copre portafogli (anche archiviati), agenti e profili di rischio con `name_warning`;
  - nomi proposti:
    - portafogli archiviati: `Portafoglio archiviato N`;
    - agenti senza portafogli assegnati: `Agente archiviato N`;
    - qualsiasi altra voce: `—`, da rinominare a mano;
  - `N` parte da 1 in ordine di `created_at` e salta i nomi già esistenti.
- **CLI `names fix`.**
  - Senza `--yes` stampa lo stesso elenco e non tocca nulla.
  - Con `--yes` applica solo i nomi proposti diversi da `—`, in una transazione, con `write_audit(method="admin:names.fix", params={"renamed": [...]})`, poi stampa `rinominati: K`.

- [ ] **Step 1: test falliti**
  - `test_fingerprint_matches_spec_words`: per ogni parola in `["Test 250 · A", "Paperclip", "Demo", "paperclip-demo", "smoke-agent", "Prova", "fake"]` → `name_warning(...) == NAME_WARNING`;
  - `test_name_warning_none_for_neutral`: `"Portafoglio Prudente"`, `"Orion"` → `None`;
  - `test_portfolio_save_returns_name_warning`: POST con nome `"Demo 2"` → 200 e `name_warning == "gli agenti vedranno questo nome"`; con `"Alpha"` → `None`. Lo stesso test per PATCH agente e profilo;
  - `test_names_audit_lists_proposals`: 2 portafogli archiviati «Test 250 · A» e «Paperclip», un agente «smoke-agent» senza portafogli, un agente attivo «demo-x» con un portafoglio → righe con `Portafoglio archiviato 1`, `Portafoglio archiviato 2`, `Agente archiviato 1` e `—` per «demo-x»;
  - `test_names_fix_requires_yes_and_audits`: senza `--yes` i nomi sono invariati; con `--yes` → 3 rinominati, «demo-x» invariato, una riga di audit `admin:names.fix`.
- [ ] **Step 2:** `cd backend && uv run pytest tests/unit/test_fingerprints.py tests/integration/test_names_cli.py -q` → FAIL.
- [ ] **Step 3:** implementare il modulo, i campi delle risposte e la CLI. Il test `test_mcp_no_fingerprints` importa `FINGERPRINT` da `smtrading.common.fingerprints` e non ha più un regex suo. Web: avviso nei 4 form, con un test Vitest per `Portfolios.tsx`: `shows name warning after save`.
- [ ] **Step 4:** PASS; `uv run pytest tests/integration/test_mcp_no_fingerprints.py tests/integration/test_admin_portfolios.py -q` e `npm --prefix web test -- --run` verdi.
- [ ] **Step 5:** commit `feat(names): shared fingerprint words, agent-visible name warning, names audit/fix CLI`
- [ ] **Step 6 (controller, non l'implementer):** in produzione si esegue solo `names audit`. L'elenco dei nomi proposti va mostrato all'utente. `names fix --yes` si esegue **solo dopo la sua approvazione**.

### Task 2: Londra, chiusure manuali, regola «un giorno di borsa»

**Files:**
- Create: `instruments/closures.py`, `migrations/versions/0024_market_closures.py`, `tests/integration/test_market_closures.py`
- Modify:
  - backend: `instruments/market_hours.py`, `models.py` (registro), `admin/routes_markets.py`, `mcp/tools_market.py` (validazione del mercato in `get_market_status`), `worker/main.py`, `api/main.py` (lifespan), `risk/live_checks.py`;
  - web: `web/src/pages/Markets.tsx`, `web/src/api/hooks/markets.ts`;
  - test: `tests/unit/test_market_hours.py`, `tests/unit/test_live_checks.py`
- Test: quelli sopra più `web/src/pages/Markets.test.tsx`

**Interfaces — Produces:**
- **Londra in `market_hours.py`:**
  - `CALENDARS["LSE"] = "XLON"`;
  - `HOURS_ONLY_MARKETS = ("LSE",)` e `STATUS_MARKETS = MARKETS + HOURS_ONLY_MARKETS`;
  - `MARKETS` resta invariato: catalogo, creazione di strumenti e `discovery.py` non accettano `LSE`, quindi i titoli `.L` restano non negoziabili;
  - `all_market_status` copre `STATUS_MARKETS`, e lo stesso vale per la validazione di `get_market_status` e di `/api/markets/status`.
- **Modello:**
  - `MarketClosure` (`market_closures`): `id BIGINT identity`; `market String(32)`, con CHECK sulle chiavi di `CALENDARS`; `day date`; `kind String(16)` con CHECK in `closed`, `early_close`; `close_time time|None` (ora locale della borsa, obbligatoria solo per `early_close`, CHECK); `note String(200)`; `created_by String(128)`; `created_at`; UNIQUE `(market, day)`;
  - `@dataclass(frozen=True) Closure`: `market: str; day: date; kind: str; close_time: time | None`.
- **Registro in memoria (`market_hours.py`):** `set_closures(rows: Iterable[Closure]) -> None`, che sostituisce l'intero registro del processo.
- **Effetto su `market_status(market, at)`**, solo per i mercati di `CALENDARS`, con la data locale della borsa (`cal.tz`):
  - giorno `closed` → chiuso tutto il giorno;
  - `early_close` → aperto fino a `close_time`, con `next_close = min(chiusura del calendario, close_time)`;
  - `next_open` salta i giorni chiusi a mano, al massimo 10 iterazioni;
  - una chiusura può solo chiudere: non apre mai un giorno di festività del calendario.
- `def previous_session_open(market: str, at: datetime) -> datetime`: l'apertura della sessione **precedente** all'ultima iniziata entro `at`, saltando calendario e chiusure manuali.
  - `CRYPTO`: `at − 24 h`.
  - `FX`: `at − 24 h`; a mercato chiuso, la chiusura settimanale meno 24 h.
- **`instruments/closures.py`:**
  - `async def refresh_closures(session) -> int`: carica le righe con `day >= oggi UTC − 7` e chiama `set_closures`;
  - `class ClosureRefresher(sessionmaker, *, interval_s: float = 60)`, con `run()`; nel worker è `supervise("market closures", …)`, nell'API è un task del lifespan;
  - `async def add_closure(session, *, market, day, kind, close_time, note, by) -> MarketClosure` e `async def delete_closure(session, closure_id) -> None`.
- **API admin:**
  - `GET /api/market-closures?from=` (default: da oggi);
  - `POST /api/market-closures` `{market, day, kind, close_time?, note}`, con 422 e `field` su mercato non ammesso, giorno passato, `early_close` senza ora o duplicato;
  - `DELETE /api/market-closures/{id}`;
  - le scritture fanno `write_audit("admin:market_closure.add|delete")`, poi `refresh_closures` nel processo API.
- **`risk/live_checks.py`:**
  - si tolgono `LAST_FILL_MAX_AGE`, `LAST_FILL_MAX_AGE_WEEKEND` e `last_fill_max_age`;
  - `_last_price(session, portfolio, instrument: Instrument, now)` usa `Fill.created_at >= previous_session_open(instrument.market, now)`;
  - il chiamante `committed_buys` passa lo strumento che già carica.
- **Pagina Mercati:** sezione «Chiusure straordinarie» con tabella (mercato, giorno, tipo «Chiusa» / «Chiusura anticipata alle HH:MM», nota, autore), modulo di inserimento ed eliminazione con conferma.
- Gli altri usi di `weekday()` nel codice (`market_hours._fx_status`, che definisce la settimana FX, e `telegram/summaries.py`, che pianifica il venerdì) non sono regole di mercato: restano.

- [ ] **Step 1: test falliti** (`test_market_hours.py`, puri)
  - `test_lse_hours_and_boxing_day`: `market_status("LSE", 2026-11-16 09:00 UTC).is_open` è vero; `2026-12-28 10:00 UTC` è chiuso;
  - `test_lse_not_tradable_market`: `"LSE" not in MARKETS`, `"LSE" in STATUS_MARKETS`;
  - `test_manual_closed_day`: con `set_closures([Closure("NYSE", date(2026,11,18), "closed", None)])`, alle 2026-11-18 15:00 UTC il mercato è chiuso e `next_open` è 2026-11-19 14:30 UTC;
  - `test_manual_early_close`: `early_close` 2026-11-18 alle `time(12)` (ET) → 16:30 UTC aperto, `next_close == 17:00 UTC`; 17:01 UTC chiuso;
  - `test_previous_session_open`, valori NYSE:
    - lun 2026-11-16 15:00 UTC → ven 2026-11-13 14:30 UTC;
    - sab 2026-11-14 12:00 UTC → gio 2026-11-12 14:30 UTC;
    - ven 2026-11-27 15:00 UTC → mer 2026-11-25 14:30 UTC (salta il Thanksgiving);
    - con chiusura manuale mer 2026-11-25 → mar 2026-11-24 14:30 UTC;
    - `CRYPTO` → `at − 24 h`;
  - `test_last_fill_uses_trading_day`, in `test_live_checks.py`: un eseguito di ven 2026-11-13 15:00 UTC conta lun 2026-11-16 15:00 UTC; uno di gio 2026-11-12 15:00 UTC no.
- [ ] **Step 2: test falliti** (`test_market_closures.py`)
  - `test_closure_crud_audited_and_refreshed`: dopo POST, `market_status` nell'API riflette la chiusura senza attendere;
  - `test_closure_validation`: mercato `CRYPTO` o `XXX`, giorno passato, `early_close` senza ora, duplicato → 422 con `field`;
  - `test_refresher_picks_up_new_rows`: `ClosureRefresher.tick()` dopo un insert diretto (Review Focus 2);
  - `test_order_waits_on_manual_closure`: un ordine a mercato di un portafoglio collegato resta `to_send` quando `BrokerSync.tick()` gira in un giorno chiuso a mano;
  - `test_mcp_market_status_includes_lse`.
- [ ] **Step 3:** `uv run pytest tests/unit/test_market_hours.py tests/unit/test_live_checks.py tests/integration/test_market_closures.py -q` → FAIL.
- [ ] **Step 4:** migrazione, modello, registro, refresher (worker e API), API, regola del giorno di borsa, pagina web (test Vitest `lists closures and adds an early close`).
- [ ] **Step 5:** PASS; `uv run pytest -q` e `npm --prefix web test -- --run` verdi.
- [ ] **Step 6:** commit `feat(markets): London hours, manual market closures, trading-day last-fill rule`

### Task 3: Campi strutturati di `log_decision`

**Files:**
- Create: `migrations/versions/0025_decision_fields.py`
- Modify: `activity/models.py`, `activity/service.py`, `mcp/tools_activity.py`, `mcp/dto.py` (`DECISION_FIELDS`), `admin/routes_telegram.py` (`GET /api/decisions`), `web/src/api/types.ts`, la tabella delle decisioni nel web (dove si usa `api/hooks/telegram.ts`)
- Test: `tests/integration/test_activity.py`, `tests/integration/test_mcp_activity_telegram.py`, `tests/integration/test_migrations.py`

**Interfaces — Produces:**
- `class DecisionOutcome(StrEnum)`: `approvata, respinta, sospesa, modificata, informativa`.
- Nuove colonne di `Decision`, tutte nullable:
  - `outcome String(16)`, con CHECK sui 5 valori o NULL;
  - `reason String(300)`;
  - `subject String(200)`.
- `log_decision(..., outcome: str | None = None, reason: str | None = None, subject: str | None = None)`:
  - testi con `strip()`; vuoto → None;
  - errori `INVALID_ORDER` con `details.field`, con i messaggi esatti:
    - `outcome must be one of approvata, respinta, sospesa, modificata, informativa`;
    - `reason must be at most 300 characters`;
    - `subject must be at most 200 characters`;
  - i campi finiscono anche nei dati dell'attività `decision.<kind>`.
- **Strumento MCP `log_decision`:** tre parametri opzionali nuovi, con la docstring aggiornata (una riga per campo, con valori e limiti). Le chiamate senza i campi funzionano come prima.
- `get_decisions` (MCP e admin) e `decision_dto`: in più `outcome`, `reason`, `subject`.

- [ ] **Step 1: test falliti**
  - `test_log_decision_structured_fields_roundtrip`;
  - `test_log_decision_without_new_fields_unchanged`;
  - `test_log_decision_field_validation`: outcome `"ok"`, reason di 301 caratteri, subject di 201 → `INVALID_ORDER` con `field`;
  - `test_mcp_get_decisions_exposes_fields`;
  - `test_admin_decisions_exposes_fields`;
  - drift delle migrazioni.
- [ ] **Step 2:** `uv run pytest tests/integration/test_activity.py tests/integration/test_mcp_activity_telegram.py tests/integration/test_migrations.py -q` → FAIL.
- [ ] **Step 3:** migrazione, modello, servizio, MCP, DTO, colonne «Esito», «Oggetto» e «Motivo» nel web.
- [ ] **Step 4:** PASS; `uv run pytest tests/integration/test_mcp_no_fingerprints.py -q` verde.
- [ ] **Step 5:** commit `feat(decisions): structured outcome, reason and subject in log_decision`

### Task 4: «Decisioni di oggi» nel riepilogo e testo delle istruzioni da approvare

**Files:**
- Modify: `telegram/summaries.py`
- Test: `tests/unit/test_telegram_render.py` (righe pure), `tests/integration/test_telegram_summaries.py`

**Interfaces — Consumes:** Task 3 (`Decision.outcome/reason/subject`).

**Interfaces — Produces:**
- `DECISION_GROUPS`: `(("strategy",), "Strategie")`, `(("risk_rule",), "Rischio")`, `(("allocation",), "Allocazioni")`, `(("veto","approval","proposal"), "Veti e approvazioni")`, `(("other",), "Altro")`.
- `OUTCOME_ICON`: `approvata ✅`, `respinta ❌`, `sospesa ⏸`, `modificata ✏️`, `informativa ℹ️`. Senza `outcome`: `approval` → ✅, `veto` → ❌, altrimenti `•`.
- `def decision_line(d: Decision, agent: str, portfolio: str | None) -> str` (pura):
  - formato `f"{icon} {ref + ' · ' if ref else ''}{subject} — {portfolio or 'generale'} — {reason} ({agent})"`;
  - `ref` = `details.get("ref")` se è un testo di al massimo 20 caratteri;
  - senza `subject` si usa `summary`, accorciato a 120 caratteri con `…`;
  - senza `reason` si toglie il segmento « — reason»;
  - uuid e `#<numero>` vengono rimossi dal testo, con il regex `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` e `#\d+`.
- **`build_summary`:**
  - sostituisce la riga «Decisioni degli agenti (N): …» con la sezione «🧭 Decisioni di oggi (N)», o «🧭 Decisioni della settimana (N)» nel settimanale, dove le righe hanno il prefisso `dd/mm`;
  - divisa per gruppo, ogni gruppo con il titolo e le sue righe;
  - al massimo 15 righe in tutto, poi `… e altre N`;
  - nessuna decisione → `Decisioni di oggi: nessuna`;
  - i portafogli archiviati restano esclusi come oggi.

- [ ] **Step 1: test falliti**
  - `test_decision_line_full`: outcome `approvata`, ref `STR-001`, subject «incrocio medie mobili 10/30 su SPY, QQQ e DIA», portafoglio «Prudente», reason «trend confermato», agente «CEO» → `"✅ STR-001 · incrocio medie mobili 10/30 su SPY, QQQ e DIA — Prudente — trend confermato (CEO)"`;
  - `test_decision_line_fallback_summary_no_ids`: senza campi, un summary di 200 caratteri con un uuid e `#42` → nessun uuid, nessun `#42`, lunghezza del soggetto 120 con `…`, icona `•`;
  - `test_daily_summary_groups_decisions`: 2 strategie, 1 veto, 1 regola di rischio → titoli «Strategie», «Rischio», «Veti e approvazioni» in quest'ordine;
  - `test_decisions_capped_at_15`: 18 decisioni → 15 righe e `… e altre 3`;
  - `test_weekly_decisions_section`.
- [ ] **Step 2:** `uv run pytest tests/unit/test_telegram_render.py tests/integration/test_telegram_summaries.py -q` → FAIL.
- [ ] **Step 3:** implementazione → PASS.
- [ ] **Step 4:** commit `feat(telegram): «Decisioni di oggi» section in daily and weekly summaries`
- [ ] **Step 5: testo per l'utente, ⚠ NON pubblicare.** L'implementer riporta nel report, senza modificare `docs/paperclip-agents/00-regole-comuni.md` né la pagina «Istruzioni agenti», la nuova versione di §9. È la §9 attuale con questo punto aggiunto dopo il secondo:

  ```markdown
  - Compila **sempre** anche questi tre campi (finiscono nel riepilogo delle 18:00 letto
    dalle persone: scrivili per una persona, senza id interni):
    - `outcome`: `approvata`, `respinta`, `sospesa`, `modificata` oppure `informativa`;
    - `subject`: cosa si decide, in chiaro, max 200 caratteri (es. «incrocio medie mobili
      10/30 su SPY, QQQ e DIA»). L'id convenzionale (`STR-001`, `PROP-…`) **non** va qui:
      resta in `details.ref`, il riepilogo lo mostra accanto;
    - `reason`: il perché in una frase, max 300 caratteri.
  ```
  Il **controller** mostra il testo all'utente. Solo dopo il suo ok lo pubblica come nuova versione delle «Regole comuni» dalla pagina «Istruzioni agenti», e aggiorna il seme `docs/paperclip-agents/00-regole-comuni.md` in un commit a parte: `docs(agents): log_decision structured fields (approved)`.

### Task 5: Ordini manuali, backend

**Files:**
- Create: `admin/routes_orders.py`, `tests/integration/test_admin_orders.py`
- Modify: `api/main.py` (router), `admin/routes_portfolios.py` (audit su `POST /api/orders/{id}/cancel`)

**Interfaces — Consumes:**
- `engine.service.preview_order`, `place_order`, `PlaceOrderRequest`;
- `admin.auth._check_totp`;
- `brokers.live.LIVE_KINDS`;
- `admin.idempotency.idempotent`.

**Interfaces — Produces:**
- `class OrderIn(BaseModel)`: `symbol: str; side: str; type: str; tif: str = "gtc"; qty: Decimal|None; notional: Decimal|None; limit_price, stop_price, trail_pct, trail_amount: Decimal|None; totp: str|None = None`.
- **`POST /api/portfolios/{id}/orders/preview`:**
  - `preview_order` con `source="admin"`, `agent_id=None`; nessun TOTP e nessuna scrittura;
  - risponde `{ok, code, message, estimated_price, estimated_notional_eur, estimated_fee, estimated_fx_fee_eur, buying_power_after_eur, real: bool, currency: <valuta base>}`;
  - `real` è vero se il portafoglio è collegato a un kind in `LIVE_KINDS`.
- **`POST /api/portfolios/{id}/orders`** (idempotente con `Idempotency-Key`, `idempotent("order")`):
  - portafoglio reale:
    - admin senza TOTP attivo → 403 «Attiva prima l'autenticazione a due fattori», `field=totp`;
    - poi `_check_totp(redis, admin, totp)`, che è anti-replay;
    - entrambi i controlli vengono **prima** di qualsiasi scrittura;
  - poi `place_order(PlaceOrderRequest(..., source="admin", agent_id=None))`, con tutti i controlli di rischio, kill switch e trading halt (passano solo le chiusure, come per gli agenti) e i controlli live per i reali;
  - un rifiuto `persisted` viene committato (l'ordine rifiutato resta registrato, come `run_tool`) e restituito come errore;
  - risponde con il DTO dell'ordine, come `GET /api/portfolios/{id}/orders`;
  - audit: `write_audit(method="admin:order.place", params={portfolio_id, symbol, side, type, qty, notional, real, order_id, status, by})`, con esito `ok` o con il codice del rifiuto.
- `POST /api/orders/{id}/cancel`: invariato, nessun TOTP, più `write_audit("admin:order.cancel")`.

- [ ] **Step 1: test falliti**
  - `test_admin_preview_runs_risk_checks`: un notional oltre `max_order_notional_eur` → `ok=false`, codice del limite, nessuna riga `orders`;
  - `test_admin_place_simulated_order_source_admin`: `source == "admin"`, `agent_id is None`, audit `admin:order.place`;
  - `test_admin_order_kill_switch_blocks_buy_allows_close` e `test_admin_order_halt_blocks_opening_allows_close`;
  - `test_admin_rejection_is_stored`;
  - `test_admin_real_order_requires_totp`, `test_admin_real_order_without_totp_enabled_forbidden` e `test_admin_real_order_totp_replay_forbidden`: nessun ordine salvato (Review Focus 5);
  - `test_admin_real_order_with_totp_goes_through_live_checks`: link `dry_run` con il `FakeLiveBroker` → l'ordine segue il percorso live e diventa `rejected LIVE_DRY_RUN` dopo `BrokerSync.tick()`;
  - `test_admin_real_preview_needs_no_totp`;
  - `test_admin_cancel_audited`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_admin_orders.py -q` → FAIL.
- [ ] **Step 3:** implementazione. Se un test di kill switch o halt mostra che il motore esenta `source="admin"`, si toglie l'esenzione solo per questi ordini e lo si annota nel report.
- [ ] **Step 4:** PASS; `uv run pytest tests/integration/test_admin_portfolios.py tests/integration/test_risk_checks.py -q` verdi.
- [ ] **Step 5:** commit `feat(admin): manual orders with preview, risk checks and TOTP on real portfolios`

### Task 6: Ordini manuali, web

**Files:**
- Create: `web/src/components/OrderForm.tsx`, `OrderForm.test.tsx`
- Modify: `web/src/pages/PortfolioDetail.tsx` (pulsante «Nuovo ordine», annulla già presente), `web/src/components/LiveOrderConfirm.tsx` (+ test), `web/src/api/hooks/portfolios.ts` (`usePreviewOrder(pid)`, `usePlaceOrder(pid)`), `web/src/api/types.ts`

**Interfaces — Consumes:** contratto HTTP del Task 5.

**Interfaces — Produces** (testi esatti):
- **`OrderForm`** (finestra «Nuovo ordine»):
  - campi:
    - Simbolo, con ricerca su `GET /api/instruments`;
    - Lato «Acquisto/Vendita»;
    - Tipo: `A mercato`, `Limite`, `Stop`, `Stop limite`, `Trailing stop`;
    - «Quantità» oppure «Controvalore», quest'ultimo solo a mercato;
    - i prezzi richiesti dal tipo;
    - Durata, con i valori di `TimeInForce`;
  - «Anteprima» chiama la preview e mostra prezzo stimato, controvalore, commissioni e liquidità dopo l'ordine, oppure il messaggio di rifiuto;
  - «Invia» si abilita solo dopo un'anteprima `ok` con gli stessi campi; cambiare un campo invalida l'anteprima.
- **Portafoglio reale:** «Invia» apre `LiveOrderConfirm`, che ora:
  - ha il titolo «🔴 Ordine con denaro reale»;
  - mostra la riga «Importo stimato» con importo e valuta;
  - ha il campo «Codice TOTP» obbligatorio, con pulsante «Invia ordine reale» disabilitato finché il campo non ha 6 cifre;
  - per un errore con `field=totp` mostra il messaggio sotto il campo;
  - per i trailing stop mantiene la nota esistente.

- [ ] **Step 1: test falliti**
  - `preview required before send`;
  - `changing a field invalidates preview`;
  - `notional only for market`;
  - `real portfolio opens live confirm with totp and amount`;
  - `totp error shown under field`;
  - `cancel button calls cancel endpoint`.
- [ ] **Step 2:** `npm --prefix web test -- --run OrderForm LiveOrderConfirm PortfolioDetail` → FAIL.
- [ ] **Step 3:** implementazione con i componenti esistenti (`FormField`, `ConfirmDialog`, `AmountPreview`).
- [ ] **Step 4:** PASS; `npm --prefix web run lint` e `npm --prefix web run build` puliti.
- [ ] **Step 5:** commit `feat(web): manual order form with preview and TOTP confirm on real portfolios`

### Task 7: Più valute nel reale

**Files:**
- Create: `migrations/versions/0026_live_fx_ref.py`
- Modify: `brokers/models.py` (`BrokerLink.fx_ref_rates`), `brokers/live/service.py` (`check_live_portfolio`, `link_live_portfolio`), `risk/live_checks.py` (`check_live_cash`), `brokers/live/reconcile.py`, `brokers/sync.py` (solo: vendite live senza cambio)
- Test: `tests/integration/test_live_service.py`, `tests/unit/test_live_checks.py`, `tests/integration/test_live_monitor.py`, `tests/integration/test_live_sync.py`

**Interfaces — Produces:**
- `BrokerLink.fx_ref_rates: JSONB | None`: `{valuta: unità della valuta del conto per 1 unità di valuta}`, l'ultimo cambio con cui la liquidità risultava in regola.
- `check_live_portfolio`: si toglie il controllo R8.5 sulla stessa valuta. Il resto resta.
- `link_live_portfolio`: con `qs` imposta `fx_ref_rates` ai cambi correnti delle valute del contante del portafoglio.
- **`check_live_cash`**, quando `snapshot currency ≠ portfolio.base_currency`:
  - `rate = min(cambio corrente valuta_conto→base, 1 / fx_ref_rates[base], se c'è)`, in unità di base per 1 unità della valuta del conto;
  - `broker_in_base = broker_cash × rate × (1 − FX_SAFETY_PCT)`, con `FX_SAFETY_PCT = Decimal("0.01")`;
  - stessa valuta → invariato.
- `FX_DRIFT_KIND = "fx_drift"`.
- **Algoritmo in `_compare` / `_record`** (nessuna sola chiusura per lo scarto di cambio):

  ```
  drift = Σ_{c ≠ ccy} |cash_c| × |rate_now[c] − fx_ref_rates.get(c, rate_now[c])|
  short = broker_cash < ours − tolerance                   # tolerance di oggi (con banda 1%)
  true_short = broker_cash < ours − tolerance − drift
  if short and not true_short and not position_diff:
      diffs += {"cash": {"fx_drift": num(drift), "ours", "broker", "currency"}}   # non azionabile
      live_alert(kind="fx_drift", summary="Scarto di cambio sul portafoglio reale: <ours> contro <broker> <ccy>")
          al massimo uno per portafoglio e giorno locale (Activity live.fx_drift da mezzanotte)
  elif true_short: come oggi (R8.7 debounce, R8.8)
  if not short and money is not None: link.fx_ref_rates = rate_now   # sotto il lock del link
  ```
  Una voce `fx_drift` non è azionabile: va esclusa in `_actionable`, e `_signature` la registra come `["cash", "fx_drift"]`.
- **Senza cambio** (Review Focus 3):
  - l'acquisto in valuta diversa attende come oggi (`_WAIT`) e scade dopo `LIVE_WAIT_MAX`;
  - una vendita live non richiede cambi nella memo di BrokerSync: le mancanze FX non la fanno attendere;
  - la riconciliazione registra `{"cash": {"skipped": NO_FX}}` come oggi.

- [ ] **Step 1: test falliti**
  - `test_link_allows_other_currency` (portafoglio EUR, conto USD);
  - `test_cash_cap_uses_worst_rate_and_margin`: portafoglio EUR, conto USD con 1000 USD. Cambio corrente 1,10 USD per EUR (0,9091 EUR per USD), riferimento `fx_ref_rates["EUR"] = 1,05`, cioè 0,9524 EUR per USD. Si usa 0,9091, quindi la liquidità del broker in EUR è `1000 / 1,10 × 0,99 = 900,00`. Un acquisto che ha bisogno di 900,01 EUR → `LIVE_CASH_LIMIT`;
  - `test_fx_only_drift_alerts_once_per_day_no_reduce_only`: due passaggi con lo stesso scarto → 1 sola attività `live.fx_drift`, portafoglio `active`;
  - `test_drift_plus_real_shortfall_trips_after_debounce`;
  - `test_fx_ref_updated_only_on_ok_pass`;
  - `test_no_fx_buy_waits_then_expires_sell_sent` e `test_reconcile_no_fx_skipped_no_alert` (Review Focus 3).
- [ ] **Step 2:** `uv run pytest tests/unit/test_live_checks.py tests/integration/test_live_service.py tests/integration/test_live_monitor.py tests/integration/test_live_sync.py -q` → FAIL.
- [ ] **Step 3:** migrazione e implementazione; aggiornare i docstring dei moduli che citano R8.5.
- [ ] **Step 4:** PASS; `uv run pytest -q` verde.
- [ ] **Step 5:** commit `feat(live): multi-currency real portfolios, worst-rate cash cap, FX-only drift alert`


**Aggiunta (decisione dell'utente 2026-11-15): deposito su portafoglio reale solo se il broker ha già il contante.**
- [ ] **Step D1: test** `tests/integration/test_live_deposit_guard.py::test_deposit_refused_when_broker_cash_short`. Il portafoglio è collegato a un link live (fake broker, conto con contante 600, portafoglio con 500). `POST /api/portfolios/{id}/deposit {amount: 200}` → 400 `INVALID_ORDER`, `details.field == "amount"`, messaggio «Il conto del broker non ha ancora questo contante: fai prima il bonifico al broker, poi registra il deposito». Saldo invariato. `{amount: 100}` → 200. Con portafoglio e conto in valute diverse il confronto avviene al cambio corrente con il margine di Task 7. Senza cambio il deposito viene rifiutato con lo stesso messaggio più «(cambio non disponibile)». `withdraw` è sempre ammesso. Portafogli non live: nessun cambiamento (test).
- [ ] **Step D2:** in `portfolios/service.py` (o in un guard chiamato da `admin/routes_portfolios.py` `deposit_cash`): se il portafoglio ha `broker_link_id` con `live_state` non null, legge il contante disponibile dal broker (snapshot fresco via `brokers/live/account.py`, come `check_live_portfolio`) e rifiuta se `contante portafoglio + deposito > contante broker`. Il messaggio è in italiano e non contiene importi del conto oltre a quelli necessari.
- [ ] **Step D3:** web: l'errore viene mostrato nel dialogo «Deposita» (test vitest). Commit insieme a Task 7.
- Con più portafogli sullo stesso conto il confronto usa la somma delle quote di tutto il collegamento: lo estende il Task 11.

### Task 8: Lettore Paperclip (costi, isolamento, errori)

**Riferimento protocollo Paperclip**, verificato il 2026-11-15:
- **Autenticazione:** `Authorization: Bearer <chiave board>`. Le chiavi hanno il prefisso `pcp_board_`. Non esistono chiavi in sola lettura: la sola lettura è garantita dal nostro client. Fonte: https://docs.paperclip.ing/reference/api/authentication/
- **Costi per agente:** `GET /api/companies/{companyId}/costs/by-agent?from=&to=`; senza date restituisce il mese UTC corrente.
- **Esecuzioni:** `GET /api/companies/{companyId}/heartbeat-runs?limit=` (default 200, massimo 1000). Ogni esecuzione ha `usageJson.costUsd`, `usageJson.cacheAdjustedCostUsd` e `usageJson.billingType`, che vale `metered_api`, `subscription_included`, `subscription_overage`, `credits`, `fixed` o `unknown`.
- **Costo addebitato:** `cacheAdjustedCostUsd` se è un numero ≥ 0, altrimenti `costUsd` se è ≥ 0, altrimenti nessuno. Fonte: https://docs.paperclip.ing/reference/api/costs/
- **Policy:** `GET /api/companies/{companyId}/tools/policies`. I campi documentati sono `policyType`, `selectors`, `conditions`, `config` e `priority`. Fonte: https://docs.paperclip.ing/reference/api/tool-gateway/
- **Da verificare all'esecuzione**, con la regola di ripiego per ciascuno:
  - forma della risposta delle policy (involucro, campo id, `name`, `enabled`): lista radice o `items`/`policies`; id da `id`, altrimenti `policyId`; se esiste `name`, si filtra sul regex `^Isolate .*SMTrading`, altrimenti si fotografano tutte;
  - campo data delle esecuzioni: `startedAt`, poi `createdAt`, poi `finishedAt`;
  - id e nome agente delle esecuzioni: `agentId`; nomi da `GET /api/companies/{companyId}/agents` (`id`, `name`); se l'endpoint non risponde, si usa l'id come nome.
  L'implementer annota nel report le risposte reali che ha letto.

**Files:**
- Create: `paperclip/__init__.py`, `paperclip/config.py`, `paperclip/client.py`, `paperclip/models.py`, `paperclip/reader.py`, `admin/routes_paperclip.py`, `migrations/versions/0027_paperclip.py`
- Create (test): `tests/unit/test_paperclip_costs.py`, `tests/integration/test_paperclip_reader.py`, `tests/integration/test_admin_paperclip.py`
- Modify: `worker/main.py` (`supervise("paperclip reader", …)`), `api/main.py` (router), `models.py`

**Interfaces — Produces:**
- **`paperclip/config.py`**, in `app_settings["paperclip"]`:
  - `@dataclass PaperclipConfig: base_url: str|None; company_id: str|None; board_key: str|None; enabled: bool = False; agent_map: dict[str, str]` (`paperclip_agent_id → smt_agent_id`), con `__repr__` senza chiave;
  - la chiave è salvata come `board_key_enc` con `aad=b"paperclip"`;
  - `load_config` / `save_config(session, *, base_url, company_id, board_key: str|None, enabled, agent_map, by)`: `board_key=None` mantiene quella salvata; `register_secret` a ogni lettura.
- `app_settings["paperclip_state"]`: `{last_ok_at, last_error, consecutive_errors, error_alerted: bool, baseline: list|None, baseline_at, isolation: "ok"|"changed"|"unknown"|"off", changes: {removed, changed, added}, alerted_signature}`.
- **`paperclip/client.py`:**
  - `class GetOnlyTransport(httpx.AsyncBaseTransport)` solleva `RuntimeError("Paperclip: solo lettura")` su ogni metodo diverso da GET, prima della rete;
  - `class PaperclipClient(cfg, *, transport=None, timeout=10.0)` con `costs_by_agent(from_, to)`, `heartbeat_runs(limit=1000)`, `agents()`, `tool_policies()`;
  - gli errori sono testi redatti della chiave: «HTTP 401», «non raggiungibile».
- **`paperclip/models.py`:** `AgentCostDay` (`agent_cost_days`), con PK `(day date, paperclip_agent_id String(64))`, `paperclip_agent_name String(128)|None`, `api_usd`, `subscription_usd`, `other_usd` NUMERIC, `runs int`, `updated_at`.
- **Costi, funzioni pure:**
  - `def billed_usd(usage: dict) -> Decimal | None`;
  - `def bucket(billing_type: str|None) -> Literal["api","subscription","other"]`: `metered_api` → api; `subscription_included|subscription_overage` → subscription; altro → other;
  - `def daily_costs(runs: list[dict], tz: str) -> dict[tuple[date, str], CostRow]`: raggruppa per giorno locale e agente, e scarta il giorno della corsa più vecchia ricevuta (giorno incompleto).
- **`class PaperclipReader(sessionmaker, *, client_factory, clock, interval_s=300)`**, con `run()` e `tick()`. Con `enabled` falso: `isolation="off"`, nessuna richiesta.
  1. **Costi:** `heartbeat_runs` → `daily_costs` → upsert delle righe `AgentCostDay`.
  2. **Policy:** si normalizzano con le chiavi `id, policyType, selectors, conditions, config, priority, enabled, name`.
     - Senza fotografia: elenco non vuoto → fotografia e `ok`; elenco vuoto → `unknown` con `last_error="nessuna regola di isolamento trovata: controlla l'id azienda e i permessi della chiave"`.
     - Con fotografia: confronto per id.
       - Rimosse, modificate (compreso `enabled` da vero a falso) → `changed`.
       - Aggiunte → solo elencate.
     - Una firma nuova di rimosse e modificate → un messaggio con `enqueue(session, source="system", category="system", level="critical", text=…)`, una volta per firma, memorizzata in `alerted_signature`.
     - Testo: `🔴 Paperclip: le regole di isolamento di SMTrading sono cambiate (N rimosse, M modificate). Controlla Paperclip, poi premi «Riconferma» nel portale.`
     - Un elenco vuoto con una fotografia esistente vale come «tutte rimosse» (Review Focus 4).
  3. **Errori:** a ogni errore `consecutive_errors += 1`. Al terzo errore, se `error_alerted` è falso, un messaggio critico: `🔴 Paperclip non raggiungibile da 3 letture: <errore>`, poi `error_alerted = True`. Un successo azzera entrambi.
  - Nessuna riga `Activity`: gli agenti non vedono nulla.
- **API admin** (`routes_paperclip.py`):
  - `GET /api/paperclip/config` → `{base_url, company_id, enabled, board_key_masked: "••••abcd"|null, agent_map}`;
  - `PUT /api/paperclip/config`, con audit `admin:paperclip.config` senza la chiave;
  - `POST /api/paperclip/verify` fa una lettura di prova (agenti e policy) e restituisce `{ok, agents: [{id, name, smt_agent}], policies_count, error}`;
  - `GET /api/paperclip/status` → `{enabled, isolation, changes, last_ok_at, last_error, consecutive_errors}`;
  - `POST /api/paperclip/baseline` fa una nuova fotografia («Riconferma»), con audit;
  - `GET /api/agent-costs?period=day|week|month` → `{currency: "USD", from, to, last_read_at, agents: [{agent_id, name, paperclip_name, api_usd, subscription_usd, other_usd, runs, orders, portfolios: [{id, name, permission, return_pct}]}], unmatched: [{paperclip_agent_id, name, api_usd, subscription_usd, other_usd}]}`;
  - abbinamento agenti: `agent_map`, altrimenti nome uguale senza distinguere maiuscole;
  - `orders`: ordini dell'agente nel periodo; `return_pct`: `get_performance(period)` con `1d|1w|1m`.

- [ ] **Step 1: test falliti** (`test_paperclip_costs.py`, puri)
  - `test_billed_prefers_cache_adjusted`;
  - `test_bucket_mapping`;
  - `test_daily_costs_drops_oldest_partial_day_and_uses_local_tz`.
- [ ] **Step 2: test falliti** (`test_paperclip_reader.py`, respx sulle 4 route GET e nessuna route POST)
  - `test_get_only_transport_refuses_post`;
  - `test_first_run_snapshots_baseline`;
  - `test_empty_policies_first_run_unknown_no_baseline`;
  - `test_empty_policies_with_baseline_alerts_all_removed` (Review Focus 4);
  - `test_changed_policy_alerts_once_per_signature`, che verifica una sola riga outbox `category="system"` dopo 3 tick;
  - `test_added_policy_listed_no_alert`;
  - `test_three_errors_alert_once_then_reset_on_success`;
  - `test_costs_upserted_per_day_agent`;
  - `test_disabled_no_requests`;
  - `test_board_key_never_in_logs_or_outbox` (`caplog`, chiave `pcp_board_CANARY0001`);
  - `test_no_activity_rows_written`.
- [ ] **Step 3: test falliti** (`test_admin_paperclip.py`)
  - `test_config_key_masked_and_kept_when_omitted`;
  - `test_verify_reports_agents_and_matches`;
  - `test_baseline_reconfirm_audited`;
  - `test_agent_costs_period_and_unmatched`.
- [ ] **Step 4:** `uv run pytest tests/unit/test_paperclip_costs.py tests/integration/test_paperclip_reader.py tests/integration/test_admin_paperclip.py -q` → FAIL.
- [ ] **Step 5:** implementazione e worker; `tests/unit/test_worker.py` aggiornato se elenca i job → PASS.
- [ ] **Step 6:** commit `feat(paperclip): read-only reader for agent costs and isolation policy alarm`

### Task 9: Paperclip, web (Impostazioni, «Costi agenti», banner)

**Files:**
- Create: `web/src/components/PaperclipSection.tsx`, `web/src/components/PaperclipBanner.tsx`, `web/src/pages/AgentCosts.tsx`, `web/src/api/hooks/paperclip.ts`, con i test Vitest accanto
- Modify: `web/src/pages/Settings.tsx` (sezione dopo «Modalità agenti»), `web/src/components/Layout.tsx` (banner accanto a `HaltBanner`), `web/src/routes.tsx` (`costi-agenti`), `web/src/lib/nav.ts` / `Sidebar.tsx` (voce «Costi agenti» sotto «Agenti»), `web/src/api/types.ts`

**Interfaces — Consumes:** contratto HTTP del Task 8.

**Interfaces — Produces** (testi esatti):
- **Sezione «Paperclip»:**
  - campi «Indirizzo», «Id azienda», «Chiave board» (password, segnaposto `••••abcd` quando è salvata), interruttore «Lettura attiva»;
  - pulsanti «Salva» e «Verifica»; l'esito di «Verifica» mostra il numero di regole e la tabella degli agenti con l'abbinamento modificabile (select degli agenti SMTrading);
  - riga di stato: «Ultima lettura: …» oppure l'errore.
- **Banner:**
  - `isolation="changed"` → barra rossa «Le regole di isolamento di Paperclip sono cambiate: N rimosse, M modificate» con il pulsante «Riconferma»;
  - `consecutive_errors >= 3` → barra gialla «Paperclip non raggiungibile».
- **Pagina «Costi agenti»:**
  - selettore «Oggi / Settimana / Mese»;
  - tabella con le colonne Agente, «A consumo API ($)», «Incluso nell'abbonamento ($)», «Altro ($)», Esecuzioni, Ordini, Portafogli (nome e rendimento %);
  - sezione «Agenti Paperclip non abbinati»;
  - importi in dollari con il formato italiano.

- [ ] **Step 1: test falliti**
  - `key never shown, only masked`;
  - `verify shows agents and mapping`;
  - `banner red on changed with reconfirm`;
  - `banner yellow after 3 errors`;
  - `costs page switches period and shows usd`.
- [ ] **Step 2:** `npm --prefix web test -- --run Paperclip AgentCosts` → FAIL.
- [ ] **Step 3:** implementazione → PASS; lint e build puliti.
- [ ] **Step 4:** commit `feat(web): Paperclip settings, isolation banner, agent costs page`

### Task 10: Modalità con il nome del broker (solo portale)

Aggiunta approvata dall'utente il 2026-11-15 (spec §Etichette delle modalità). Task piccolo e separato: tocca solo le DTO admin e il web, non gli strumenti MCP.

**Files:**
- Modify:
  - backend: `portfolios/mode.py` (`mode_label`, `admin_mode_fields`), `admin/routes_portfolios.py` (`_portfolio_dto`), `admin/routes_dashboard.py` (righe e `mode_counts`), `admin/routes_agents.py` (portafogli degli agenti);
  - web: `web/src/lib/portfolioMode.ts`, `web/src/components/PortfolioModeBadge.tsx`, `web/src/components/ModeBanner.tsx`, `web/src/pages/Dashboard.tsx` (`ModeCounts`), `web/src/api/types.ts`, i 5 punti che usano `PortfolioModeBadge` (`AgentPortfoliosSummary.tsx`, `AgentDetail.tsx`, `Dashboard.tsx`, `Portfolios.tsx`, `PortfolioDetail.tsx`)
- Create (test): `tests/unit/test_portfolio_mode.py`
- Test: `tests/integration/test_admin_portfolios.py`, `tests/integration/test_admin_brokers.py` (oggi verifica `mode` sulle DTO e la Dashboard), `tests/integration/test_mcp_portfolio.py`, `web/src/components/PortfolioModeBadge.test.tsx`, `web/src/pages/Dashboard.test.tsx`, `web/src/components/Layout.test.tsx` (banner)

**Interfaces — Produces:**
- **`portfolios/mode.py`** (le funzioni esistenti restano identiche):
  - `BROKER_NAMES = {"alpaca": "Alpaca", "t212": "Trading 212", "etoro": "eToro"}`, chiave = prefisso del kind prima di `_`;
  - `PRACTICE_WORD = {"alpaca_paper": "prova", "t212_demo": "demo", "etoro_demo": "demo"}`;
  - `SIMULATED_LABEL = "SMTrading (simulato)"`;
  - `def mode_label(portfolio: Portfolio, link: BrokerLink | None) -> str` (pura):
    - nessun collegamento → `SIMULATED_LABEL`;
    - kind in `PRACTICE_WORD` → `f"{nome} · {parola}"`, per esempio «Alpaca · prova», «Trading 212 · demo», «eToro · demo»;
    - kind in `LIVE_KINDS` → `f"🔴 {nome} · reale"`, più « · prova a secco» con `dry_run` e « · sospeso» con `configured`;
    - kind sconosciuto → il kind così com'è;
  - `def admin_mode_fields(portfolio, link) -> dict`: `{**mode_fields(portfolio, link), "broker_kind": link.kind if link else None, "mode_label": mode_label(portfolio, link)}`.
- **DTO admin:** `_portfolio_dto`, le righe della Dashboard e i portafogli degli agenti usano `admin_mode_fields` al posto di `mode_fields`. `mcp/tools_portfolio.py` continua a usare `mode_fields`: **nessun campo nuovo verso gli agenti**.
- **`GET /api/dashboard`:** campo nuovo `mode_counts: [{label, count, live: bool}]`, raggruppato per `mode_label`, in quest'ordine: simulati, poi prova e demo in ordine alfabetico, poi reali. `GET /api/dashboard/modes` resta invariato (lo usa il banner).
- **Web:**
  - `api/types.ts`: `broker_kind: string | null` e `mode_label: string` nei tipi che oggi hanno `mode`; `PortfolioMode` resta;
  - `PortfolioModeBadge` riceve `label` e mostra il testo esatto di `mode_label`, senza un'icona in più (il 🔴 è già nel testo dei reali); colore da `mode` e `liveState` come oggi (verde, giallo, rosso per ogni kind reale); `aria-label` `Modalità: <label>. <hint>`;
  - `MODE_INFO` tiene colori e suggerimenti; le etichette di ripiego, per una risposta senza `mode_label`, diventano «SMTrading (simulato)», «Conto di prova del broker» e «🔴 Reale»;
  - `ModeCounts` della Dashboard: `«{count} {testo}»` separati da « · », dove il testo è «simulati» («simulato» per 1) per la voce simulata e l'etichetta senza « · » per le altre, per esempio «2 simulati · 1 Alpaca prova · 0 reali». Senza voci reali si aggiunge «0 reali»;
  - `ModeBanner`, testi nuovi: «SMTrading (simulato) e conti di prova dei broker – nessun denaro reale» e «SMTrading (simulato) – denaro virtuale, prezzi di mercato reali». Il testo rosso dei reali non cambia.

- [ ] **Step 1: test falliti** (backend)
  - `test_mode_label_table`: parametrizzato su nessun link, `alpaca_paper`, `t212_demo`, `etoro_demo`, `alpaca_live` nei 3 `live_state`, `t212_live` armato, `etoro_live` in `dry_run` → le etichette esatte sopra (per esempio «🔴 eToro · reale · prova a secco»);
  - `test_admin_portfolio_dto_has_mode_label`: `GET /api/portfolios` → `mode_label` e `broker_kind`, con `mode` e `live_state` invariati;
  - `test_dashboard_mode_counts`: 2 simulati e 1 `alpaca_paper` → `mode_counts == [{"label": "SMTrading (simulato)", "count": 2, "live": false}, {"label": "Alpaca · prova", "count": 1, "live": false}]`;
  - `test_agent_payloads_unchanged_by_mode_labels`, in `test_mcp_portfolio.py`: un portafoglio simulato, uno `alpaca_paper` e uno su un link live in `dry_run`, con `mcp_hide_mode` acceso e spento. Le risposte di `list_portfolios` e `get_portfolio` non contengono le chiavi `mode_label` e `broker_kind` né i testi «SMTrading (simulato)», «Alpaca», «· prova», «· demo». Dove compare, `mode` è uguale a `mode_fields(...)["mode"]`.
- [ ] **Step 2:** `uv run pytest tests/unit/test_portfolio_mode.py tests/integration/test_admin_portfolios.py tests/integration/test_admin_brokers.py tests/integration/test_mcp_portfolio.py -q` → FAIL.
- [ ] **Step 3: test falliti** (web)
  - `badge shows broker label`: «Trading 212 · demo» in giallo, «🔴 Alpaca · reale · sospeso» in rosso;
  - `dashboard counts per broker label`: il testo «2 simulati · 1 Alpaca prova · 0 reali»;
  - `banner simulated and practice text`.
  `npm --prefix web test -- --run PortfolioModeBadge Dashboard Layout` → FAIL.
- [ ] **Step 4:** implementazione → PASS; `uv run pytest tests/integration/test_mcp_no_fingerprints.py -q` verde, invariato; `npm --prefix web run lint` e `npm --prefix web run build` puliti.
- [ ] **Step 5:** commit `feat(web): portfolio mode labels with the broker name (portal only)`

### Task 11: Più portafogli reali su un conto, backend

Aggiunta approvata dall'utente il 2026-11-15 (spec §Più portafogli reali su un conto). Toglie il vincolo «un collegamento reale ↔ un portafoglio» del piano 8. **Nessuna migrazione:** su `portfolios.broker_link_id` non c'è un vincolo UNIQUE (verificato su modelli e migrazioni 0009-0021); la regola 1:1 è solo in `gate.py` (`len(portfolios) != 1`) e in `check_live_portfolio` («il collegamento reale è già usato da un altro portafoglio»).

**Files:**
- Create: `brokers/live/quotas.py`, `tests/integration/test_live_multi_portfolio.py`
- Modify:
  - `brokers/live/gate.py` (portafoglio dell'ordine tra quelli collegati; docstring);
  - `brokers/live/service.py` (`check_live_portfolio`, `link_live_portfolio`, `_require_fresh_empty_test`, dettagli dell'audit `armed`; docstring);
  - `risk/live_checks.py` (`check_live_cash`, `check_live_fuse`, `live_orders_sent`, `max_live_orders_per_hour`; docstring);
  - `brokers/sync.py` (`_live_memo`: cambi del contante degli altri portafogli del collegamento; `_live_refusal` passa il link al fusibile);
  - `brokers/live/reconcile.py` (portafogli coinvolti per simbolo, debounce per simbolo, scarto di cambio per collegamento);
  - la guardia sui depositi di Task 7 (Step D2);
  - `admin/routes_live.py` (`_live_dto`: `portfolios`, quote, contante non assegnato)
- Test: `tests/integration/test_live_gate.py`, `tests/integration/test_live_service.py`, `tests/unit/test_live_checks.py`, `tests/integration/test_live_monitor.py`, `tests/integration/test_live_sync.py`, `tests/integration/test_admin_live.py`, `tests/integration/test_live_deposit_guard.py`

**Interfaces — Consumes:** Task 7 (`FX_SAFETY_PCT`, `fx_ref_rates`, `FX_DRIFT_KIND`, guardia sui depositi D1-D3).

**Interfaces — Produces:**
- **`brokers/live/quotas.py`:**
  - `@dataclass(frozen=True) class Quota: portfolio_id: UUID; name: str; quota: Decimal` (valuta del conto);
  - `async def portfolio_quota(session, qs, portfolio, link) -> Decimal`: il contante del portafoglio (`get_cash_balances`) nella valuta del conto. Per una valuta `c` diversa si usa il cambio peggiore per il conto, cioè il più alto tra corrente e `fx_ref_rates[c]` se c'è, moltiplicato per `1 + FX_SAFETY_PCT`. Senza cambio: `SMTError(PROVIDER_UNAVAILABLE)`;
  - `async def link_quotas(session, qs, link, *, include: Portfolio | None = None, exclude: UUID | None = None) -> list[Quota]`: i portafogli collegati (`Portfolio.broker_link_id == link.id`), più `include` se non è ancora collegato, meno `exclude`; ordine per nome;
  - `def unassigned(broker_cash: Decimal, quotas: list[Quota]) -> Decimal`: `broker_cash − Σ quota`, anche negativo.
- **Collegamento** (`check_live_portfolio`, `link_live_portfolio`):
  - si toglie il rifiuto «il collegamento reale è già usato da un altro portafoglio»;
  - `_require_fresh_empty_test` diventa `_require_fresh_test(link, now, *, empty: bool)`. `link_live_portfolio` chiede `empty=True` solo se il collegamento non ha ancora portafogli; altrimenti valgono età del test, impronta e conto cash, ma non il conto vuoto. `arm_live_link` resta con `empty=True`, come oggi;
  - con il conto non vuoto, `check_live_portfolio` chiede che l'ultima `Reconciliation` di ogni portafoglio già collegato sia `ok` e degli ultimi `LIVE_TEST_MAX_AGE_S`. Altrimenti `_invalid("riconciliazione non in regola: verifica il conto prima di collegare un altro portafoglio", "reconcile")`;
  - il controllo `ours > broker_cash` diventa `Σ link_quotas(include=portfolio) > cash_available` dello snapshot → `_invalid("la somma delle quote dei portafogli supera la liquidità disponibile al broker", "cash", quotas_total=…, broker_cash=…, unassigned=…)`. Senza cambio: lo stesso campo, con «cambio non disponibile per confrontare la liquidità»;
  - TOTP, ordine dei lock (link, poi portafoglio, R8.9), `check_relink` (il portafoglio nuovo senza posizioni) e audit `portfolio_linked` restano. Scollegare resta per portafoglio e rifiutato con posizioni reali aperte, come oggi;
  - `arm_live_link`: `details.portfolios` elenca tutti gli id collegati; `portfolio_id` dell'audit resta il primo.
- **Cancello (`LiveGateTransport._check`)**, al posto del blocco `limit(2)`, con lo stato letto di nuovo come oggi:
  - nessun portafoglio collegato → `LIVE_NOT_ARMED`;
  - con `smt_order_id`: ordine non trovato, o il suo `portfolio_id` non è tra i collegati → `LIVE_NOT_ARMED` con `OTHER_PORTFOLIO_MESSAGE`; il suo portafoglio `locked` o `archived` → `PORTFOLIO_LOCKED`; halt e kill switch si valutano sul portafoglio dell'ordine, come oggi;
  - senza `smt_order_id`: passa solo se almeno un portafoglio collegato non è bloccato né archiviato e nessuno è sotto halt;
  - tutte le altre condizioni del piano 8 restano: `armed`, prova a secco, kill switch con le sole chiusure `live_flatten`, halt, DELETE sempre ammessi.
- **Acquisti (`check_live_cash`)**, importi nella valuta base del portafoglio:
  - `others = Σ link_quotas(link, exclude=portfolio.id)`, in valuta del conto. È la quota riservata degli altri: il loro contante, che contiene ancora il costo dei loro acquisti aperti finché non sono eseguiti. Così nulla si conta due volte;
  - `broker = to_base(cash_available − others)`, con il cambio peggiore e il margine di Task 7;
  - `available = min(ours, broker) − committed`, come oggi; nei dettagli c'è anche `others_reserved`;
  - snapshot vecchio → solo il contante del portafoglio, come oggi (BrokerSync ricontrolla con dati freschi);
  - un cambio mancante per la quota di un altro portafoglio → `PROVIDER_UNAVAILABLE`, che in BrokerSync diventa `_WAIT`, come oggi;
  - `_live_memo` precarica anche le coppie di cambio del contante degli altri portafogli collegati, verso la valuta del conto.
- **Vendite:** `check_no_short` invariato, sulle sole posizioni del portafoglio.
- **Ordine con esito sconosciuto (M8):** `_mark_unknown` mette in sola chiusura solo `order.portfolio_id` (già così). L'ordine resta aperto, quindi conta nei `committed_buys` del suo portafoglio e la quota riservata di quel portafoglio non scende finché non si chiarisce.
- **Fusibile, per collegamento:**
  - `live_orders_sent(session, link_id, now, *, exclude)` conta gli ordini con `Order.broker_link_id == link_id`;
  - il limite è il più basso tra i `max_live_orders_per_hour` dei portafogli collegati (default 30);
  - al superamento, `trip_reduce_only(cause="fuse")` per ogni portafoglio collegato;
  - `check_live_fuse(session, *, portfolio, link, now, …)`: i due chiamanti hanno già il link.
- **Disattivazione, prova a secco, kill switch, cambio di conto:** valgono già per collegamento (`_settle_disarm` e `account_changed` scorrono `_linked_portfolios`). I test lo confermano con due portafogli.
- **Riconciliazione:**
  - `_State.holders: dict[str, set[UUID]]`: per simbolo, i portafogli con una posizione diversa da zero o un ordine aperto al broker;
  - `_compare` aggiunge a ogni voce di simbolo `"portfolios": [id, …]`, cioè i `holders` del simbolo, oppure tutti i collegati se nessuno lo ha;
  - `_record(pid)` registra le voci di simbolo che includono `pid` più la voce del contante;
  - **debounce per simbolo:** sono confermate le voci azionabili presenti anche nella riga precedente dello stesso portafoglio, stessa chiave e stesso segno (`_actionable` come insieme). Si va in `reconcile_diff` se c'è almeno un simbolo confermato, in `cash_shortfall` se il contante è confermato. Nei dettagli solo le voci confermate;
  - contante: `ours` è già la somma di tutti i portafogli collegati, quindi una mancanza mette in sola chiusura tutti i portafogli;
  - scarto di cambio (Task 7): un solo avviso per collegamento e giorno locale (la ricerca dell'`Activity` `live.fx_drift` filtra sul collegamento, non sul portafoglio);
  - R8.10 (`margin_account`) e cambio di conto: tutti i portafogli, come oggi.
- **Guardia sui depositi** (Task 7, D2): si rifiuta se `Σ link_quotas(link) + deposito`, convertito con `portfolio_quota`, supera `cash_available`. Il messaggio di D1 resta.
- **`_live_dto`** (`GET /api/live-links` e le risposte delle azioni):
  - `portfolio` è sostituito da `portfolios: [{id, name, status, base_currency, quota, positions_count}]`, ordinati per nome;
  - campi nuovi: `currency`, `broker_cash`, `quotas_total`, `unassigned`;
  - `quota`, `quotas_total` e `unassigned` sono `null` senza snapshot o senza cambio;
  - la rotta riceve `qs: Quotes`; le letture dei cambi avvengono fuori da lock e transazioni (R9).
- I docstring di `gate.py`, `brokers/live/service.py` e `risk/live_checks.py` che citano «1 live link <-> 1 portfolio» si aggiornano.

- [ ] **Step 1: test falliti** (`test_live_multi_portfolio.py`, `FakeLiveBroker`, conto 1000.00 USD, portafogli USD salvo dove indicato)
  - `test_link_second_portfolio_when_quotas_fit`: A (500) collegato, B (400) → collegato con TOTP, audit `portfolio_linked`, `unassigned == 100`;
  - `test_link_third_portfolio_refused_when_quotas_exceed_cash`: poi C (200) → 422 `field=cash`, C non collegato, nessuna riga di audit (Review Focus 6);
  - `test_link_second_portfolio_requires_reconciled_account`: A ha posizioni e l'ultima riconciliazione non è `ok` → 422 `field=reconcile`; dopo un passaggio `ok` → collegato;
  - `test_second_portfolio_foreign_currency_quota_worst_rate`: B in EUR, la quota usa il cambio più alto tra corrente e riferimento, più l'1%;
  - `test_buy_cannot_spend_other_portfolios_quota`: A 500 e B 400 collegati su 1000, poi il contante del broker scende a 800 → un acquisto di B che richiede 310 → `LIVE_CASH_LIMIT` (800 − 500 = 300), 290 → accettato; lo stesso esito in `BrokerSync.tick()` con uno snapshot fresco (Review Focus 6);
  - `test_other_portfolios_open_buy_not_counted_twice`: A 500 con un acquisto aperto al broker da 400, B 400, broker 1000 → B può impegnare 400 (la quota di A vale 500, non 900);
  - `test_unknown_order_trips_only_its_portfolio_and_stays_committed`: esito sconosciuto su un acquisto di A → A in sola chiusura, B `active`; la quota di A non scende finché l'ordine è aperto;
  - `test_sell_limited_to_own_holdings`: A 3 AAPL, B 2 AAPL; B vende 3 → `SHORT_NOT_ALLOWED`;
  - `test_reconcile_same_symbol_sum_ok`: 3 + 2 contro un broker con 5 → `ok` per A e B;
  - `test_reconcile_symbol_diff_trips_only_holders`: A 3 AAPL, B 2 AAPL, C solo contante; broker con 4 per due passaggi → A e B `reduce_only` (`live_reconcile_diff`), C `active`; al primo passaggio nessuno (Review Focus 6);
  - `test_reconcile_unknown_symbol_trips_all`: MSFT al broker e in nessun portafoglio → tutti in sola chiusura al secondo passaggio;
  - `test_reconcile_debounce_per_symbol`: passaggio 1 diff AAPL, passaggio 2 diff AAPL e MSFT → solo i portafogli con AAPL; passaggio 3 → anche quelli con MSFT;
  - `test_cash_shortfall_trips_all_linked`: contante del broker sotto la somma delle quote oltre la tolleranza per due passaggi → A, B e C in sola chiusura;
  - `test_fx_drift_alert_once_per_link_per_day`: due portafogli, solo scarto di cambio → una sola `live.fx_drift`, nessuna sola chiusura;
  - `test_disarm_stops_all_linked_portfolios`: ordini aperti di A e B → quelli mai inviati rifiutati, gli altri con richiesta d'annullamento; il cancello rifiuta poi un POST per entrambi (Review Focus 6);
  - `test_dry_run_and_kill_switch_apply_to_all_linked`;
  - `test_fuse_counts_link_orders_and_trips_all`: limiti 5 e 3, 2 ordini inviati da A e 1 da B → il quarto, di B, è rifiutato e A e B vanno in sola chiusura;
  - `test_gate_accepts_any_linked_portfolio`: A bloccato, un ordine di B passa; un ordine di B bloccato → `PORTFOLIO_LOCKED`; un ordine di un portafoglio non collegato → `LIVE_NOT_ARMED` con `OTHER_PORTFOLIO_MESSAGE`; nessuna chiamata al trasporto interno;
  - `test_deposit_guard_uses_sum_of_quotas`: A 400, B 300, broker 1000 → deposito su A di 300 ammesso, di 301 rifiutato con il messaggio di D1;
  - `test_unlink_one_portfolio_keeps_the_others`: B senza posizioni si scollega, A resta collegato e armato; A con posizioni → «Chiudi prima le posizioni reali»;
  - `test_live_dto_lists_portfolios_quotas_unassigned`, in `test_admin_live.py`;
  - `test_live_status_counts_two_armed_portfolios`: `GET /api/live/status` elenca A e B con `live_state="armed"`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_live_multi_portfolio.py -q` → FAIL.
- [ ] **Step 3:** implementazione. I test esistenti che fissano il vincolo 1:1 (per esempio il rifiuto «già usato da un altro portafoglio», o il cancello chiuso con due portafogli) si riscrivono sulle regole nuove; nel report si elencano uno per uno.
- [ ] **Step 4:** PASS; `uv run pytest tests/integration/test_live_gate.py tests/integration/test_live_service.py tests/unit/test_live_checks.py tests/integration/test_live_monitor.py tests/integration/test_live_sync.py tests/integration/test_admin_live.py tests/integration/test_live_deposit_guard.py tests/integration/test_mcp_no_fingerprints.py -q` verdi, poi `uv run pytest -q` verde. `test_mcp_no_fingerprints` non cambia.
- [ ] **Step 5:** commit `feat(live): several real portfolios per account with cash quotas, per-symbol reconciliation`

### Task 12: Più portafogli reali su un conto, web

**Files:**
- Create: `web/src/components/LiveLinkPortfolios.tsx`, `LiveLinkPortfolios.test.tsx`
- Modify: `web/src/pages/Brokers.tsx` (scheda del collegamento reale), `web/src/api/types.ts` (`LiveLink.portfolios`, `currency`, `broker_cash`, `quotas_total`, `unassigned`; via `portfolio`), `web/src/api/hooks/live.ts` (collega e scollega per portafoglio), ogni uso di `LiveLink.portfolio` nel web (`git grep -n "\.portfolio\b" web/src`)
- Test: `web/src/pages/Brokers.test.tsx`, `web/src/components/Layout.test.tsx` (banner)

**Interfaces — Consumes:** contratto HTTP del Task 11 (`_live_dto`, `PUT /api/portfolios/{id}/live-link`), etichette del Task 10.

**Interfaces — Produces** (testi esatti):
- **`LiveLinkPortfolios`**, nella scheda del collegamento reale:
  - titolo «Portafogli collegati»; tabella con Portafoglio (badge del Task 10), Stato, «Quota» (importo e valuta del conto, «—» se `null`) e l'azione «Scollega»;
  - «Scollega» è disabilitato con `positions_count > 0`, con il suggerimento «Chiudi prima le posizioni reali»; senza posizioni chiede conferma e chiama `PUT /api/portfolios/{id}/live-link` con `link_id: null`;
  - sotto la tabella: «Contante al broker: X», «Assegnato ai portafogli: Y», «Non assegnato: Z». Un valore negativo di Z è in rosso, con «le quote superano il contante del conto»;
  - pulsante «Collega un portafoglio», attivo solo in `dry_run` e `armed`: select dei portafogli non collegati e non archiviati, poi `LiveTotpDialog`. Un errore `field=cash` o `field=reconcile` mostra il messaggio nella finestra.
- **Banner:** `liveBannerText` conta già i portafogli armati, quindi con due portafogli il testo è «Denaro reale attivo su 2 portafogli».

- [ ] **Step 1: test falliti**
  - `lists linked portfolios with quota and unassigned`;
  - `link another portfolio asks totp`;
  - `quota error shown in dialog`;
  - `unlink disabled with open positions`;
  - `banner counts two live portfolios`.
- [ ] **Step 2:** `npm --prefix web test -- --run LiveLinkPortfolios Brokers Layout` → FAIL.
- [ ] **Step 3:** implementazione con i componenti esistenti (`ConfirmDialog`, `LiveTotpDialog`, `PortfolioModeBadge`) → PASS.
- [ ] **Step 4:** `npm --prefix web test -- --run`, `npm --prefix web run lint` e `npm --prefix web run build` puliti.
- [ ] **Step 5:** commit `feat(web): live link card lists linked portfolios with quotas and unassigned cash`

### Task 13: Protocollo eToro, adattatore demo, server finto

**Riferimento protocollo eToro.** L'implementer usa solo questo; fonti consultate il 2026-11-15, indice su https://api-portal.etoro.com/llms.txt.

- **Base:** `ETORO_URL = "https://public-api.etoro.com"`. Versioni miste: `/api/v1` per portafoglio, chiusure e dati di mercato; `/api/v2` per ordini e consultazione. Le aperture v1 `by-amount`, `by-units` e `limit-orders` sono **ritirate dal 2026-08-18** (https://api-portal.etoro.com/core/changelog.md): non si usano.
- **Autenticazione:** a ogni richiesta `x-api-key`, `x-user-key` e `x-request-id` (un UUID). Ogni chiave vale per un solo ambiente, Demo o Real; i permessi sono Read o Write (https://api-portal.etoro.com/core/getting-started/authentication.md).
- **Percorsi** (demo | reale):

  | Operazione | Demo | Reale |
  |---|---|---|
  | Apertura | `POST /api/v2/trading/execution/demo/orders` | `POST /api/v2/trading/execution/orders` |
  | Stato dell'ordine | `GET /api/v2/trading/info/demo/orders:lookup?orderId=` (oppure `referenceId=`) | `GET /api/v2/trading/info/orders:lookup?…` |
  | Annullamento | `DELETE /api/v2/trading/execution/demo/orders/{orderId}` | `DELETE /api/v2/trading/execution/orders/{orderId}` |
  | Chiusura | `POST /api/v1/trading/execution/demo/market-close-orders/positions/{positionId}` | `POST /api/v1/trading/execution/market-close-orders/positions/{positionId}` |
  | Stato della chiusura | `GET /api/v1/trading/info/demo/close-orders/{orderId}` | `GET /api/v1/trading/info/real/close-orders/{orderId}` |
  | Portafoglio | `GET /api/v1/trading/info/demo/pnl` | `GET /api/v1/trading/info/real/pnl` |
  | Idoneità | `POST /api/v2/trading/info/demo/eligibility` | `POST /api/v2/trading/info/eligibility` |

  Fonti: `api-reference/trading--demo/*` e `api-reference/trading--real/*` sotto https://api-portal.etoro.com/.
  - L'annullamento vale solo per gli ordini in attesa di mercato o di trigger.
  - La chiusura parziale è ammessa con `UnitsToDeduct`.
  - Il corpo dell'idoneità è `{instrumentIds, currency:"USD"}`.
  - Condivisi e solo GET: `GET /api/v1/market-data/search?internalSymbolFull=…&fields=…`, `GET /api/v1/market-data/instruments/rates?instrumentIds=`.
- **Corpo d'apertura** (camelCase): `{"action":"open","transaction":"buy","instrumentId":<int>,"settlementType":"real","orderType":"mkt","leverage":1, "units":<qty>}` oppure `"amount":<USD>` al posto di `units`.
  - `orderCurrency` ammette solo `usd`.
  - Risposta 200 `{token, orderId, referenceId}`: l'ordine è accettato, non eseguito.
  - Ordini `mit` e `limitIOC` esistono ma **non** si mappano (vedi le regole sotto).
- **Corpo di chiusura** (PascalCase): `{"InstrumentID":<int>,"UnitsToDeduct":<units>|null}`, dove `null` chiude tutta la posizione. Risposta `{orderForClose:{positionID, instrumentID, unitsToDeduct, orderID, statusID, …}, token}`.
- **Consultazione dell'ordine:**
  - campi: `status{id,name,errorCode,errorMessage}`, `asset{instrumentId, settlementType, leverage, side}`, `positionExecutions[]{positionId, openingData{units, avgPrice, fees}}`, `totalCosts`;
  - stati: 1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled, 6 PendingCancel, 7 Canceled, 8 Expired, 9 CanceledPartiallyFilled, 10 RejectedPartiallyFilled, 11 WaitingForMarket, 12 PendingTriggeredRate.
- **Portafoglio (`clientPortfolio`):**
  - campi: `credit` (USD), `accountCurrencyId` (1 = USD), `positions[]`, `orders[]`, `ordersForOpen[]`, `ordersForClose[]`, `mirrors[]`;
  - ogni posizione: `positionID`, `instrumentID`, `isBuy`, `units`, `openRate`, `amount`, `leverage`, `mirrorID`, `settlementTypeID` (1 = bene reale), `totalFees`;
  - lo schema e gli esempi differiscono nelle maiuscole (`positionID` / `positionId`): si leggono entrambe le forme;
  - liquidità disponibile = `credit − Σ ordersForOpen[].amount (mirrorID == 0) − Σ orders[].amount` (https://api-portal.etoro.com/core/guides/calculate-available-cash.md).
- **Strumenti:**
  - `search` restituisce `items[]{instrumentId, internalSymbolFull, isCurrentlyTradable, isBuyEnabled, isDelisted}`, anche con corrispondenze parziali: si sceglie quella con `internalSymbolFull` identico;
  - l'idoneità restituisce `allowOpenPosition`, `allowPartialClosePosition`, `leverageConfigs[]{settlementType, direction, leverageValues}` e cambia di rado.
- **Limiti di frequenza:** per chiave utente, su una finestra di 60 s.
  - Scritture (aperture, chiusure, annullamenti): 20.
  - Consultazione degli ordini: 60.
  - Portafoglio: 60.
  - Dati di mercato: 120 secondo le pagine degli endpoint, 60 secondo la pagina generale (vedi il Task 14, che usa 60).
  - Idoneità: 20.
  - Intestazioni: `RateLimit-Remaining`, `RateLimit-Reset` (secondi). Un 429 ha `Retry-After` (https://api-portal.etoro.com/core/getting-started/rate-limits.md).
- **Idempotenza:** `x-request-id` è la chiave d'idempotenza dell'apertura e torna come `referenceId`. Un'apertura con esito perso si cerca con `orders:lookup?referenceId=`.
- **Regole fissate dal piano**:
  - **Tipi d'ordine:** `supported_types = {market}`. Limit, stop, stop limit e trailing al broker sono rifiutati all'instradamento con `INVALID_ORDER` «tipo d'ordine non disponibile su questo broker: solo ordini a mercato». I trailing stop simulati da SMTrading passano come oggi, a mercato al trigger.
  - **Dimensione:** `qty` → `units`. `notional` → `amount` solo per strumenti in USD; altrimenti non instradabile, con lo stesso messaggio e «solo quantità».
  - **Idempotenza:** `idempotent_place = False`, come R7: nessun reinvio. `x-request-id` dell'apertura vale `uuid5(ETORO_NS, client_ref(order))` e `find_order` usa `orders:lookup?referenceId=`; per le vendite `find_order` restituisce None. Le altre richieste hanno un `uuid4` nuovo.
  - **Vendita:** posizioni conformi dello strumento in ordine FIFO.
    - Le posizioni intere fino alla quantità si chiudono con `UnitsToDeduct: null`.
    - L'ultima si chiude parzialmente solo se `allowPartialClosePosition`.
    - Al massimo 10 chiusure per ordine.
    - `broker_order_id = f"close:{qty}:{id1},{id2},…"`; `get_order` aggrega gli stati delle chiusure.
    - Tutte finali → `filled` se `filled ≥ qty`, altrimenti `canceled` con il parziale. Se nulla è chiudibile → `rejected` «quantità inferiore alla posizione chiudibile».
    - Un errore dopo almeno una chiusura accettata restituisce le chiusure accettate: il resto diventa un parziale e la riconciliazione copre l'eventuale scarto.
  - **Stati:** 1, 2, 6, 11, 12 → `accepted`; 3 → `filled`; 5 → `partially_filled`; 4 → `rejected`; 7 e 9 → `canceled`; 8 → `expired`; 10 → `canceled` (gli eseguiti restano).
  - **Eseguito:** quantità = Σ `openingData.units`, prezzo = media ponderata di `avgPrice`, `fees` = Σ `openingData.fees` (USD).
  - **Posizione conforme:** `isBuy`, `leverage == 1`, `settlementTypeID == 1`, `mirrorID == 0`. Nello snapshot `account_type = "cash"` se tutte le posizioni sono conformi, altrimenti `"leveraged"`. Un ordine consultato con `asset.leverage ≠ 1` o `settlementType ≠ real` → `rejected` con un log ERROR.
  - **Simboli:** prima `instrument.provider_symbols["etoro"]`; altrimenti cripto `BTC-USD` → `BTC`; altrimenti il simbolo così com'è. Uno strumento non idoneo (non compra a leva 1 `real`, `isBuyEnabled` falso o non trovato) non è instradabile: «strumento non negoziabile senza leva su questo broker».
- **Da verificare all'esecuzione**, ciascuno con la sua regola di ripiego:
  1. **Campi di `close-orders/{orderId}`** (unità eseguite, prezzo, stato). Se mancano unità o prezzo, l'eseguito aspetta (`FILL_PRICE_WAIT`, nessun prezzo inventato) e l'implementer si ferma e lo segnala al controller prima del Task 16.
  2. **Ordine FIFO delle posizioni.** Si usa `openDateTime` se presente nelle posizioni, altrimenti `positionID` crescente.
  3. **Identità del conto per l'impronta.** Si usa l'id utente di `api-reference/identity/get-authenticated-user-profile`, se risponde con un id; altrimenti `sha256(user_key)[:16]` (una chiave vale per un solo conto e ambiente).
  4. **Valute del conto diverse da USD.** Solo `accountCurrencyId == 1` (USD). Un altro valore fa fallire il test del collegamento con «valuta del conto non supportata».
  5. **`settlementType: "real"` per la cripto.** Decide l'idoneità (`leverageConfigs`); senza una configurazione `real`, buy, leva 1, lo strumento non è instradabile.
  6. **`internalSymbolFull` delle azioni non USA** (per esempio `SAP.DE`). Si usa la mappatura per strumento (`provider_symbols["etoro"]`), modificabile nella pagina Mercati.

**Files:**
- Create: `brokers/etoro_core.py`, `brokers/etoro_demo.py`, `tests/integration/test_brokers_etoro.py`, `tests/fixtures/brokers/etoro_*.json`
- Modify: `brokers/live/fake.py` (route eToro), `tests/conftest.py` (`_no_live_network` blocca anche `hostname(ETORO_URL)`), `tests/integration/test_brokers_protocol.py` (parametrizzato anche su `EtoroDemoAdapter`)

**Interfaces — Produces:**
- `etoro_core.py`: `ETORO_URL`, `ETORO_NS: uuid.UUID` (costante fissa), `class _EtoroProtocol(BrokerAdapter)`.
  - ClassVar della classe concreta:
    - `ENV: Literal["demo","real"]`;
    - `EXEC_V1, EXEC_V2, INFO_V1, INFO_V2: str` (prefissi);
    - `ALLOWED_PREFIXES: tuple[str, ...]`;
  - `supported_types = frozenset({OrderType.market})`;
  - asset class: azioni USA e UE, ETF, cripto (come `AssetClass`).
  - `def _guard_path(self, method: str, path: str) -> None`, chiamata da `_request` prima di ogni richiesta. Solleva `SMTError(FORBIDDEN, "percorso non ammesso per questo collegamento")` se:
    - il percorso non inizia con un prefisso di `ALLOWED_PREFIXES`;
    - `ENV == "demo"` e il percorso non è di market data né contiene `/demo/`;
    - `ENV == "real"` e il percorso contiene `/demo/`;
    - è una richiesta non GET su market data.
  - Ogni richiesta d'ordine porta `extensions={"smt_order_id": str(order.id)}`. Una chiusura porta anche `"smt_close": {"symbol", "instrument_id", "units"}`.
  - Cache strumenti per ambiente, come `t212_core.instrument_cache`: simbolo → `{instrument_id, eligible: bool, partial_close: bool}`, TTL 24 h.
  - **Parti di modulo riusate dal provider dati del Task 14** (senza stato d'adattatore, nessun percorso d'esecuzione):
    - `MARKET_DATA_PREFIX = "/api/v1/market-data/"`;
    - `etoro_headers(api_key: str, user_key: str, request_id: str | None = None) -> dict[str, str]` (un `uuid4` nuovo se `request_id` manca);
    - `instrument_cache(scope: str) -> InstrumentCache`, con `scope` in `"demo"`, `"real"`, `"data"`;
    - `async def search_instrument(http, headers: dict, symbol: str) -> dict | None`: la `search` con `internalSymbolFull` e la scelta della corrispondenza identica (regola «Strumenti» sopra), oppure None;
    - `rate_headers(resp) -> tuple[int | None, float | None]`: `RateLimit-Remaining` e `RateLimit-Reset`, oppure `Retry-After` su un 429;
    - `http_error(resp) -> str`: «etoro: HTTP <codice>» più il messaggio del corpo redatto, mai intestazioni né chiavi.
  - Rate limit: attende `RateLimit-Reset` quando `RateLimit-Remaining == 0` (sleep iniettabile, massimo 60 s); un 429 → `RateLimited`.
  - Errori: 401/403 → `RuntimeError("HTTP 401 …")`; 400/422 in apertura → `BrokerOrder(status="rejected", message=<messaggio redatto>)`.
  - Metodi: `place_order`, `find_order`, `get_order`, `cancel_order`, `get_positions` (solo le posizioni conformi, sommate per simbolo), `get_account` (`AccountSnapshot`, valuta `USD`), `prepare` e `health_check` (GET del portafoglio).
- `etoro_demo.py`: `EtoroDemoAdapter(_EtoroProtocol)`, `kind = "etoro_demo"`, `BASE_URL = ETORO_URL`, `ENV = "demo"`, prefissi demo e market data; config `api_key`, `user_key`. È registrato in `registry._load()`.
- **`FakeLiveBroker`:**
  - risponde ai percorsi eToro dei due ambienti, con stato separato per ambiente: conto vuoto, `credit` 1000.00 USD;
  - strumenti AAPL (1001), MSFT (1002) e BTC (100000), tutti idonei buy leva 1 `real`; `allowPartialClosePosition` configurabile;
  - le aperture `mkt` creano una posizione e sono eseguite subito al `price`; le chiusure riducono le posizioni;
  - `close-orders/{id}` restituisce `{statusID, unitsDeducted, closeRate}`, **forma assunta dal piano** (da allineare al punto 1 «da verificare»);
  - `calls: list[tuple[str, str]]` registra metodo e percorso.

- [ ] **Step 1: test falliti** (`test_brokers_etoro.py`, sul `FakeLiveBroker.transport()` o su respx)
  - `test_buy_payload_is_unleveraged_buy`: corpo esatto con `transaction="buy"`, `leverage=1`, `settlementType="real"`, `orderType="mkt"`, `units`, e intestazioni `x-api-key`, `x-user-key`, `x-request-id = uuid5(ETORO_NS, client_ref)`;
  - `test_payload_never_short_or_leveraged`: parametrizzato su acquisti e vendite, nessun corpo contiene `sellShort`, `leverage ≠ 1` o un altro `settlementType`;
  - `test_notional_amount_only_usd`;
  - `test_non_market_types_not_routable_message`;
  - `test_sell_fifo_partial_close_allowed`;
  - `test_sell_without_partial_close_closes_whole_positions_rest_partial` (Review Focus 1): 3 posizioni da 1, vendita di 2,5 → 2 chiusure intere, `get_order` → `canceled` con `filled_qty == 2`;
  - `test_status_mapping_table` (gli stati da 1 a 12);
  - `test_demo_adapter_cannot_call_real_path`: con un percorso reale forzato su `_request` → `FORBIDDEN` e nessuna voce nuova in `calls`;
  - `test_demo_paths_all_contain_demo` (tutte le `calls` non di market data);
  - `test_account_snapshot_cash_and_empty`: `credit` meno gli ordini in apertura;
  - `test_nonconforming_position_marks_leveraged`;
  - `test_find_order_by_reference_id`;
  - `test_ratelimit_waits_and_429_retryable`;
  - `test_symbol_mapping_override_and_crypto`;
  - `test_ineligible_instrument_not_routable`;
  - `test_socket_guard_blocks_etoro_host`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_brokers_etoro.py tests/integration/test_brokers_protocol.py -q` → FAIL.
- [ ] **Step 3:** implementazione → PASS; `uv run pytest tests/integration/test_brokers_t212.py tests/integration/test_brokers_alpaca.py -q` verdi.
- [ ] **Step 4:** commit `feat(brokers): eToro protocol core with per-path guard, demo adapter, fake eToro server`

### Task 14: eToro come fonte di dati di mercato (connettore `etoro`)

Decisione dell'utente del 2026-11-15 (spec §eToro come fonte di dati): le quotazioni e le barre eToro sono una fonte **generale**, usabile per tutti i portafogli, con attivazione e priorità nella pagina «Connettori». Non dipende dai collegamenti eToro.

**Riferimento dati di mercato eToro.** Fonti consultate il 2026-11-15, indice su https://api-portal.etoro.com/llms.txt.
- **Verificato:**
  - **Autenticazione:** a ogni richiesta `x-request-id` (UUID) più `x-api-key` e `x-user-key` insieme, oppure `Authorization: Bearer` OAuth, mai entrambi. Nessun accesso con la sola chiave API. Ogni chiave vale per un solo ambiente (Demo o Real); i permessi sono Read o Write (https://api-portal.etoro.com/core/getting-started/authentication.md). Il connettore usa solo la coppia di chiavi.
  - **Quotazioni:** `GET /api/v1/market-data/instruments/rates?instrumentIds=1,2,3`, al massimo **100** id per chiamata. Risposta `rates[]{instrumentID, bid, ask, lastExecution, conversionRateBid, conversionRateAsk, date, priceRateID}`; i campi `unitMargin*` e `*Discounted` sono obsoleti e non si leggono (https://api-portal.etoro.com/api-reference/market-data/get-instrument-market-rates.md).
  - Esiste anche `GET /api/v2/market-data/rates` (fino a 1000 id, `results[]{instrumentId, bid, ask, date, quoteType: realtime|delayed}`, 206 se solo una parte è disponibile), senza `lastExecution` (https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments.md). Il piano usa la v1, già citata nel Task 13 e con l'ultimo prezzo.
  - **Barre:** `GET /api/v1/market-data/instruments/{instrumentId}/history/candles/{direction}/{interval}/{candlesCount}`, senza parametri di data. `direction` = `asc|desc`; `interval` = `OneMinute, FiveMinutes, TenMinutes, FifteenMinutes, ThirtyMinutes, OneHour, FourHours, OneDay, OneWeek`; `candlesCount` al massimo 1000. Risposta `{interval, candles:[{instrumentId, candles:[{instrumentID, fromDate, open, high, low, close, volume}], rangeOpen, …}]}`: maiuscole diverse tra gruppo e candela; `fromDate` ISO 8601, con `Z` negli esempi (https://api-portal.etoro.com/api-reference/market-data/get-instrument-candle-history.md).
  - **Strumenti:** `GET /api/v1/market-data/search?internalSymbolFull=AAPL&fields=…` (`fields` obbligatorio); può restituire corrispondenze parziali, quindi si accetta solo `internalSymbolFull` identico; gli id non cambiano mai e si mettono in cache (https://api-portal.etoro.com/core/guides/get-instrument-id.md, https://api-portal.etoro.com/api-reference/market-data/search-for-instruments.md). Una ricerca senza risultati risponde 200 con `items` vuoto. Lo schema `Instrument` **non** ha un campo valuta.
  - **Metadati:** `GET /api/v1/market-data/instruments?instrumentIds=` restituisce `instrumentDisplayDatas[]{instrumentID, instrumentDisplayName, instrumentTypeID, exchangeID, symbolFull, priceSource, …}`, anche qui senza valuta (https://api-portal.etoro.com/api-reference/market-data/get-instrument-display-data.md). Non serve a questo task.
  - **Limiti:** le pagine degli endpoint indicano **120 richieste ogni 60 s, quota condivisa** tra gli endpoint di market data, con le intestazioni `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (secondi), `RateLimit-Policy` (`120;w=60`) e `Retry-After` su un 429. La pagina generale dice invece **60 al minuto** per le letture `GET`, dati di mercato compresi, **per chiave utente**, su finestra mobile di 1 minuto (https://api-portal.etoro.com/core/getting-started/rate-limits.md). Le due fonti non coincidono: il piano usa la più prudente, 60.
  - WebSocket di quotazioni e candele esistono (`core/websocket/…`), ma non si usano in questo task (niente `stream`).
- **Da verificare all'esecuzione**, ciascuno con la sua regola di ripiego:
  1. **Una chiave Demo con permesso Read legge i dati di mercato.** La guida del connettore consiglia una chiave **Demo, Read**: non tocca denaro. Se «Verifica» risponde 401/403 con una chiave Demo valida, la guida passa a una chiave **Real, Read**: il connettore resta limitato ai `GET` di market data, quindi non può leggere il portafoglio né negoziare.
  2. **Valuta dei prezzi.** Nessuno schema ha la valuta. Si accetta una quotazione solo se `conversionRateBid` e `conversionRateAsk` mancano o valgono 1 (prezzo già in USD); altrimenti la quotazione si salta. Per questo il connettore copre solo azioni USA e cripto in USD.
  3. **`date` delle quotazioni ritardate.** Se per le azioni USA `date` è l'ora del prezzo, `STALE_QUOTE` fa il resto. Se invece è l'ora della risposta e i prezzi sono ritardati, l'implementer lo segnala al controller e il connettore resta in fondo alle priorità delle azioni USA.
  4. **`fromDate` senza fuso.** Se arriva senza `Z` né offset, si legge come UTC.
  5. **Id degli strumenti uguali tra Demo e Real.** La cache dati è separata (`instrument_cache("data")`) e non si scambia con quella dei collegamenti, quindi la risposta non cambia il codice.

**Decisioni del piano:**
- **Chiavi proprie, non quelle dei collegamenti eToro:**
  - i limiti valgono per chiave utente: una chiave condivisa farebbe competere dati e ordini;
  - basta il permesso Read;
  - il connettore vale per tutti i portafogli e sopravvive allo scollegamento;
  - le chiavi `etoro_live` restano solo dietro il cancello del piano 8.
- **Configurazione:** `api_key` e `user_key` nel connettore, cifrate da `update_connector` come le altre e mostrate solo mascherate (`••••` più le ultime 4 cifre).
- **Il router non aspetta:** a quota esaurita il provider fallisce subito e il router passa alla fonte successiva. A differenza del Task 13, nessuna `sleep`.
- **Nasce spento e in fondo alle priorità** di `us_equity` e `crypto` per `quote` e `bars`. Finché l'admin non lo attiva, niente cambia.

**Files:**
- Create:
  - `marketdata/providers/etoro.py`;
  - `backend/migrations/versions/0028_etoro_connector.py`;
  - test: `tests/unit/providers/test_etoro.py`, `tests/fixtures/marketdata/etoro_search.json`, `etoro_rates.json`, `etoro_candles.json` (forme del riferimento sopra).
- Modify:
  - backend: `marketdata/providers/__init__.py` (import), `marketdata/provider.py` (`config_fields`), `connectors/service.py` (`DEFAULT_PRIORITIES`), `admin/routes_connectors.py` (`config_fields` nella DTO), `brokers/live/fake.py` (rates e candele sui due ambienti);
  - test: `tests/integration/test_connectors_service.py`, `tests/integration/test_admin_connectors.py`, `tests/integration/test_router.py`, `tests/integration/test_mcp_no_fingerprints.py` (variante `etoro_source`);
  - web: `web/src/api/types.ts` (`Connector.config_fields`), `web/src/pages/Connectors.tsx` (campi da `config_fields`, etichette e guida eToro), `web/src/pages/Connectors.test.tsx`.

**Interfaces — Consumes:** Task 13 (`ETORO_URL`, `MARKET_DATA_PREFIX`, `etoro_headers`, `instrument_cache`, `search_instrument`, `rate_headers`, `http_error`); router, `acquire` e connettori esistenti (`marketdata/router.py`, `marketdata/ratelimit.py`, `connectors/service.py`).

**Interfaces — Produces:**
- **`DataProvider.config_fields: ClassVar[tuple[str, ...]] = ()`:** i campi di configurazione che il portale mostra anche se vuoti. La DTO del connettore aggiunge `config_fields: list[str]`; `secret_keys` comprende anche i `config_fields` con `is_secret`. Gli altri provider non cambiano: `requires_key` continua ad aggiungere `api_key`.
- **`@register class EtoroProvider(DataProvider)`** in `marketdata/providers/etoro.py`:
  - `name = "etoro"`, `capabilities = {quote, bars}`, `asset_classes = {us_equity, crypto}`, `requires_key = True`, `config_fields = ("api_key", "user_key")`, `default_rate_limit_per_min = 60`;
  - `_default_symbol`: `us_equity` → il simbolo; `crypto` `XXX-USD` → `XXX`; il resto → `ProviderUnsupported`. L'override è `instrument.provider_symbols["etoro"]`, la stessa chiave del broker (Task 13), modificabile nella pagina Mercati;
  - `_get(path, params)`, l'unico punto che tocca la rete:
    - prima della rete: `path` deve iniziare con `MARKET_DATA_PREFIX`, altrimenti `RuntimeError("etoro: percorso non ammesso")`; il metodo è sempre `GET` (il modulo non ha altri metodi);
    - senza `api_key` o `user_key` → `ProviderUnsupported("request without API key")`, come Finnhub;
    - quota: registro di modulo `_blocked_until[sha256(user_key)[:16]]`. Se è nel futuro → `RuntimeError("etoro: rate limited")` senza rete. Dopo una risposta con `RateLimit-Remaining == 0` si blocca fino a `RateLimit-Reset`; un 429 blocca fino a `Retry-After` e fallisce. Blocco massimo 60 s;
    - 401/403 → `RuntimeError("etoro: chiave non valida")`; altri ≥ 400 → `RuntimeError(http_error(resp))`; `httpx.RequestError` → `RuntimeError(f"etoro: request failed: {type(exc).__name__}")` (`from None`);
    - JSON letto con `parse_float=Decimal`.
  - `_instrument_ids(pairs)`: id dalla cache `instrument_cache("data")`, TTL 24 h. Al massimo `MAX_LOOKUPS_PER_CALL = 10` ricerche nuove per chiamata, con `search_instrument`; gli altri simboli restano senza quotazione in questo giro. Un simbolo non trovato o `isDelisted` entra in una cache negativa per 1 h.
  - `get_quotes(items)`: id a blocchi di 100 su `instruments/rates`. Per ogni `rates[]` con `instrumentID` noto: `Quote(bid, ask, last=lastExecution, ts=date, provider="etoro")`. Si salta la voce se `lastExecution` manca o è ≤ 0, se `date` manca (mai `now()`, come gli altri provider), o se un `conversionRate*` presente è diverso da 1 (regola 2).
  - `get_bars(symbol, ps, timeframe, start, end)`:
    - `INTERVALS = {1m: OneMinute, 5m: FiveMinutes, 15m: FifteenMinutes, 1h: OneHour, 1d: OneDay, 1wk: OneWeek}`;
    - `count = ceil((now − start) / durata) + 1`. Oltre 1000 → `ProviderUnsupported("range")`: il router passa alla fonte successiva;
    - una sola richiesta `…/history/candles/desc/{interval}/{count}`. Si leggono le candele del gruppo con `instrumentId` uguale (entrambe le maiuscole), si tengono quelle con `start ≤ fromDate < end`, in ordine crescente, con `Decimal`. Si saltano le candele con OHLC mancante;
    - strumento non trovato → `ProviderUnsupported`.
  - `health_check()`: `search_instrument("AAPL")`; senza risultato → `RuntimeError("etoro: strumento di prova non trovato")`.
- **`connectors/service.py`:** `"etoro"` **in coda** a `DEFAULT_PRIORITIES` per `(us_equity, quote)`, `(us_equity, bars)`, `(crypto, quote)`, `(crypto, bars)`. Non è in `DEFAULT_ENABLED`; `_ALL_CONNECTORS` lo include da sé.
- **`0028_etoro_connector.py`** (solo dati, `down_revision = "0027"`):
  - upgrade: inserisce la riga `connectors` `etoro` spenta (`ON CONFLICT DO NOTHING`); aggiunge `"etoro"` in coda alle quattro righe di `provider_priorities` che esistono e non lo contengono già. I valori di `asset_class` e `capability` sono quelli scritti da `str_enum`, da controllare su `0003_connectors.py`. Il seed non lo farebbe, perché non tocca le righe esistenti;
  - downgrade: toglie `"etoro"` da tutte le righe di priorità e cancella la riga del connettore.
- **`FakeLiveBroker`** (Task 13): risponde anche a `instruments/rates` e a `history/candles/…` per AAPL, MSFT e BTC, con `conversionRate*` 1 e `date` dato dal fake; `market_data_remaining: int | None` configurabile per le intestazioni di quota. Gli stessi percorsi rispondono su tutti e due gli ambienti.
- **Portale, pagina «Connettori»:**
  - i campi della scheda sono l'unione delle chiavi di `config_masked` e di `config_fields`;
  - etichette «Chiave API (x-api-key)» e «Chiave utente (x-user-key)»;
  - sotto la scheda `etoro`, la guida: «chiave dedicata, ambiente Demo, permesso Read; non riusare le chiavi dei collegamenti», con il link a https://api-portal.etoro.com/core/getting-started/authentication.md;
  - priorità e limite al minuto con i controlli che ci sono già (riordino nella matrice, campo «limite»).
- **Agenti:** il nome della fonte `etoro` compare nelle quotazioni come gli altri. Gli errori del provider non contengono percorsi né parole da simulazione: il modulo non scrive mai `/demo/`.

- [ ] **Step 1: test falliti** (`tests/unit/providers/test_etoro.py` con respx, `assert_all_mocked=True`, chiavi canarino `CANARYAPIKEY0001` e `CANARYUSERKEY-do-not-use`)
  - `test_headers_and_market_data_path_only`: intestazioni `x-api-key`, `x-user-key`, `x-request-id` (UUID) su ogni richiesta;
  - `test_non_market_data_path_refused_before_network`: un percorso `/api/v1/trading/…` forzato su `_get` → errore e nessuna richiesta;
  - `test_symbol_mapping_us_crypto_override`;
  - `test_instrument_lookup_exact_match_and_cache`: la ricerca restituisce `AAPL` e `AAPL.US`, si sceglie l'identico; la seconda chiamata non ricerca; un simbolo assente non si ricerca prima di 1 h;
  - `test_lookups_capped_per_call`;
  - `test_quotes_batch_of_100_and_mapping`: 150 simboli → due richieste, `last = lastExecution`, `ts = date`;
  - `test_quote_skipped_without_date_or_non_usd_conversion`;
  - `test_bars_interval_count_and_window`: `1h` su 3 giorni → `OneHour`, `count` corretto, solo le candele nella finestra, in ordine crescente, maiuscole miste;
  - `test_bars_over_1000_candles_unsupported`;
  - `test_ratelimit_remaining_zero_blocks_without_sleep` e `test_429_retry_after_blocks` (Review Focus 7): nessuna `sleep`, la chiamata seguente fallisce senza rete fino al reset;
  - `test_errors_redacted_no_keys`: 401, 500 e `ConnectError` → messaggi senza chiavi, senza intestazioni, senza `demo`.
- [ ] **Step 2: test falliti** (integrazione)
  - `test_connectors_service`: `etoro` seminato spento e in coda alle quattro priorità;
  - `test_admin_connectors`: `config_fields` e `secret_keys` di `etoro`, chiavi mascherate in lettura, «Verifica» sul fake con le chiavi canarino, nessuna chiave in `last_error` né nei log (`caplog`);
  - `test_router`: con `etoro` attivo e primo, una quotazione AAPL arriva da `etoro`; con quota esaurita il router usa subito la fonte successiva;
  - migrazione: upgrade e downgrade di `0028` su un database con priorità già modificate dall'admin (l'ordine dell'admin resta, `etoro` va solo in coda); `test_migration_matches_models` verde;
  - `test_mcp_no_fingerprints[etoro_source]`: la fonte delle quotazioni del fixture è `etoro`, nessuna parola vietata e nessun `/demo/` negli output.
- [ ] **Step 3:** `uv run pytest tests/unit/providers/test_etoro.py tests/integration/test_connectors_service.py tests/integration/test_admin_connectors.py tests/integration/test_router.py tests/integration/test_mcp_no_fingerprints.py -q` → FAIL.
- [ ] **Step 4:** implementazione, web compreso (test Vitest `etoro connector shows api and user key fields masked with guide`) → PASS; `uv run pytest -q` e `npm --prefix web test -- --run` verdi.
- [ ] **Step 5:** commit `feat(marketdata): eToro market-data provider (quotes, bars) as a general connector`. Il connettore resta **spento**: le chiavi e l'attivazione sono dell'utente, dalla pagina «Connettori», dopo il deploy.

### Task 15: eToro demo nel portale, in BrokerSync e per gli agenti

**Files:**
- Modify:
  - backend: `admin/routes_brokers.py` (`KINDS["etoro_demo"] = (ETORO_URL, ("api_key","user_key"))`, segreti, messaggio del kind, conto vuoto al collegamento), `mcp/dto.py` (`_broker_texts`);
  - test: `tests/integration/test_mcp_no_fingerprints.py` (variante `etoro_linked`);
  - web: `web/src/lib/broker.ts` («eToro — prova», chiavi `api_key`, `user_key`; l'etichetta di modalità «eToro · demo» c'è già dal Task 10), `web/src/pages/Brokers.tsx` (guida: chiave demo e azzeramento del conto virtuale dall'app eToro prima del collegamento)
- Test: `tests/integration/test_admin_brokers.py`, `tests/integration/test_brokers_sync.py`

**Interfaces — Consumes:** Task 13.

**Interfaces — Produces:**
- **Collegamento di un portafoglio a `etoro_demo`** (`PUT /api/portfolios/{id}/broker-link`):
  - prima della transazione: `open_adapter`, poi `get_account()` (solo GET);
  - posizioni o ordini aperti → 422 con `NOT_DEDICATED_MESSAGE` (da `brokers.live.service`) e `field=account`.
- **`_broker_texts`:** coppie in più:
  - il kind `etoro_demo` → `etoro_live`;
  - i prefissi demo → i reali, presi dalle ClassVar `EXEC_V1/EXEC_V2/INFO_V1/INFO_V2` delle due classi;
  - import pigro di `brokers.live.etoro_live`: finché il Task 16 non esiste, la coppia dei kind si salta (nessun errore).
- **BrokerSync:** nessun percorso nuovo. Il test conferma che una vendita composta passa da `get_order` fino a `canceled` con il parziale e che le commissioni vengono da `BrokerOrder.fees`.

- [ ] **Step 1: test falliti**
  - `test_create_etoro_demo_link_keys_masked`;
  - `test_link_refused_when_demo_account_not_empty`;
  - `test_sync_etoro_demo_buy_fill_with_broker_fee`;
  - `test_sync_etoro_sell_composite_partial`;
  - `test_mcp_no_fingerprints[etoro_linked]`: nessuna parola vietata, nessun `/demo/` e nessun `etoro_demo` negli output.
- [ ] **Step 2:** `uv run pytest tests/integration/test_admin_brokers.py tests/integration/test_brokers_sync.py tests/integration/test_mcp_no_fingerprints.py -q` → FAIL.
- [ ] **Step 3:** implementazione, web compreso (test Vitest `etoro demo kind with two keys and guide`).
- [ ] **Step 4:** PASS; `uv run pytest -q` e `npm --prefix web test -- --run` verdi.
- [ ] **Step 5:** commit `feat(brokers): eToro demo links in the portal, dedicated account check, agent-safe texts`

### Task 16: eToro reale dietro il cancello (costruito, spento)

**Files:**
- Create: `brokers/live/etoro_live.py`, `tests/integration/test_etoro_live.py`
- Modify:
  - `brokers/live/__init__.py` (`LIVE_KINDS` + `etoro_live`; `LIVE_PATHS`; `CASH_ONLY_KINDS`);
  - `brokers/registry.py`;
  - `brokers/live/account.py` (`LIVE_FIELDS["etoro_live"] = ("user_key", "api_key", frozenset())`);
  - `brokers/live/gate.py` (corrispondenza delle chiusure eToro);
  - `brokers/live/service.py` (`_cash_account`, validazione delle chiavi);
  - `brokers/live/reconcile.py` (controllo `CASH_ONLY_KINDS`);
  - `admin/routes_live.py` (`LiveKeysIn.user_key`, `_KEY_FIELDS`, messaggio del kind);
  - test: `tests/unit/test_live_hosts_guard.py` (percorsi reali), `tests/integration/test_mcp_no_fingerprints.py` (variante `etoro_live_dry_run`);
  - web: `web/src/lib/live.ts`, `web/src/components/LiveLinkWizard.tsx` («eToro — denaro reale», guida chiavi)

**Interfaces — Consumes:** Task 13 (`_EtoroProtocol`), piano 8 (`require_live_construction`, `make_live_client`, `LiveGateTransport`, `trip_reduce_only`).

**Interfaces — Produces:**
- `EtoroLiveAdapter(_EtoroProtocol)`:
  - `kind = "etoro_live"`, `live = True`, `idempotent_place = False`, `BASE_URL = ETORO_URL`, `ENV = "real"`;
  - prefissi reali, l'**unico** file del repository fuori da `docs/` che li scrive;
  - `__init__` → `require_live_construction` (cancello obbligatorio).
- **`brokers/live/__init__.py`:**
  - `LIVE_HOSTS` resta calcolato da Alpaca e Trading 212: l'host eToro è condiviso con la demo e la sua guardia è il percorso;
  - `LIVE_PATHS: frozenset[str]` = i prefissi d'esecuzione e d'info reali di `EtoroLiveAdapter` (import pigro);
  - `CASH_ONLY_KINDS = frozenset({"alpaca_live", "etoro_live"})`.
- **Cancello:** `_body_matches` riconosce una chiusura eToro, cioè un corpo con `InstrumentID` o `InstrumentId` e l'estensione `smt_close`. Corrisponde se:
  - `instrument_id` dell'estensione è uguale a quello del corpo;
  - il simbolo dell'estensione corrisponde all'ordine;
  - `order.side == sell` e `0 < units ≤ order.qty`.
  Questo vale per «Chiudi tutte le posizioni reali» a kill switch acceso e per le chiusure sotto halt. Il dry run intercetta il primo `POST` (apertura o chiusura) con `LiveDryRun`.
- **R8.10 generalizzato:**
  - `_cash_account(kind, snapshot)` vero se `kind not in CASH_ONLY_KINDS` oppure `snapshot.account_type == "cash"`;
  - in `reconcile` il controllo di oggi per Alpaca vale per `CASH_ONLY_KINDS`: una posizione non conforme → sola chiusura (`margin_account`) e avviso critico.
- **Conto dedicato:** valgono test, prova a secco, attivazione e collegamento del piano 8 (NOT_DEDICATED, TOTP, Telegram, fusibile, riconciliazione, impronta con la regola 3 del Task 13, disattivazione in due fasi), con le regole di più portafogli per conto del Task 11.
- **Guardia del repository:** `test_live_paths_only_in_brokers_live` scandisce i file tracciati, esclusi `docs/**` e `backend/src/smtrading/brokers/live/**`, con pattern costruiti da `LIVE_PATHS`. Il regex di ciascun prefisso non deve colpire la variante demo: per esempio `trading/execution/(?!demo/)`, costruito senza scrivere il percorso nel test.
- **Wizard** (opzione «eToro — denaro reale»):
  - campi «Chiave API (x-api-key)» e «Chiave utente reale (x-user-key)»;
  - guida: chiave utente creata per l'ambiente **Real** con permesso **Write**; non riusare la chiave demo; conto senza posizioni, ordini o copy trading;
  - link a https://api-portal.etoro.com/core/getting-started/authentication.md.

- [ ] **Step 1: test falliti** (`test_etoro_live.py`, cancello su un link del db di test, respx con `assert_all_mocked`/`assert_all_called` oppure il fake)
  - `test_live_requires_gate_client` e `test_live_rejects_other_base_url`;
  - `test_live_adapter_cannot_call_demo_path`;
  - `test_live_paths_never_contain_demo`;
  - `test_demo_adapter_never_reaches_real_paths_even_on_live_client`;
  - `test_dry_run_intercepts_open_and_close`;
  - `test_kill_switch_flatten_close_passes_only_with_matching_extension`;
  - `test_armed_buy_is_unleveraged_and_audited`;
  - `test_nonconforming_position_trips_margin_account`;
  - `test_activation_flow_etoro_with_canary_keys`: chiavi → test → dry run → arm (TOTP) → link → disarm, sul fake;
  - `test_etoro_canary_keys_never_leak` (risposte, `live_audit`, audit, outbox, `broker_raw`, log);
  - `test_live_paths_only_in_brokers_live` e `test_guard_detects_planted_real_path`;
  - `test_mcp_no_fingerprints[etoro_live_dry_run]`: un rifiuto per dry run arriva agli agenti come un rifiuto del broker, e nessun `etoro_live` o percorso demo compare.
- [ ] **Step 2:** `uv run pytest tests/integration/test_etoro_live.py tests/unit/test_live_hosts_guard.py tests/integration/test_mcp_no_fingerprints.py -q` → FAIL.
- [ ] **Step 3:** implementazione, web compreso (test Vitest `wizard offers etoro live with key guide`).
- [ ] **Step 4:** PASS; `uv run pytest -q` e `npm --prefix web test -- --run` verdi; `git grep -n "etoro_live" -- backend/src | grep -v brokers/live` mostra solo i registri attesi (`__init__`, `account`, `registry`, `dto`, `routes_live`).
- [ ] **Step 5:** commit `feat(live): eToro real-money adapter behind the live gate, per-path guards, wizard option`

### Task 17: E2E, documentazione e verifica finale

**Files:**
- Create: `web/e2e/orders.spec.ts`
- Modify: `web/e2e/live.spec.ts` (variante eToro sul `FakeLiveBroker`), `README.md`, `docs/paperclip-mcp.md`

**Interfaces — Consumes:** tutto il piano.

**Interfaces — Produces:**
- `orders.spec.ts`: «Nuovo ordine» su un portafoglio simulato → anteprima → invio → ordine in elenco → «Annulla» su un limite aperto.
- `live.spec.ts`: la procedura eToro con le chiavi canarino → test → prova a secco → attiva (TOTP) → collega → un ordine manuale con `LiveOrderConfirm` e TOTP → «Disattiva denaro reale».
- `live.spec.ts`, più portafogli: un secondo portafoglio collegato con TOTP → la scheda mostra due quote e il contante non assegnato → il badge mostra «🔴 eToro · reale» → il banner dice «Denaro reale attivo su 2 portafogli».
- **`README.md`**, sezioni nuove:
  - «eToro»: demo e reale, conto dedicato, solo leva 1, solo ordini a mercato, guida delle chiavi, commissioni FX configurabili nelle regole di commissione;
  - «Chiusure di borsa»;
  - «Ordini manuali»;
  - «Più valute nel reale» (scarto di cambio = solo avviso);
  - «Paperclip: costi e isolamento»;
  - «Pulizia dei nomi» (`names audit|fix`);
  - «Più portafogli reali su un conto»: quote, contante non assegnato, sola chiusura per simbolo o per tutti, controlli validi per tutto il collegamento;
  - «Etichette delle modalità» (solo portale, gli agenti non vedono nulla);
  - «eToro come fonte di dati»: connettore `etoro` per tutti i portafogli, chiave dedicata Demo e Read (o Real e Read, se la Demo non legge i dati), solo azioni USA e cripto in USD, spento e in coda finché l'admin non lo attiva e lo riordina;
  - la checklist di attivazione del piano 8, con `etoro_live` tra i kind. Nessun percorso d'esecuzione reale scritto fuori da `docs/`.
- **`docs/paperclip-mcp.md`:**
  - i campi `outcome`, `reason` e `subject` di `log_decision`;
  - `LSE` in `get_market_status` (solo orari: i titoli londinesi non sono negoziabili);
  - il messaggio «tipo d'ordine non disponibile su questo broker: solo ordini a mercato»;
  - nessun cenno a eToro nelle istruzioni agenti.

- [ ] **Step 1:** scrivere le due spec → `bash scripts/e2e.sh` → FAIL finché helper e seed non coprono eToro (`devtools/live_e2e_seed.py` invariato salvo ciò che serve).
- [ ] **Step 2:** helper e documentazione → `bash scripts/e2e.sh` verde.
- [ ] **Step 3: verifica**
  1. `cd backend && uv run pytest -q` → verde, nessun test live o eToro saltato;
  2. `npm --prefix web test -- --run`, `npm --prefix web run lint` e `npm --prefix web run build` → verdi;
  3. `uv run pytest tests/unit/test_live_hosts_guard.py tests/integration/test_mcp_no_fingerprints.py -q` → PASS;
  4. deploy da un worktree pulito di HEAD: `docker compose run --rm migrate`, poi `docker compose up -d --no-build --no-deps api worker web`; `alembic current` = `0028`;
  5. `docker compose exec -T api smtrading live status` → «nessun collegamento live»; `SELECT count(*) FROM broker_links WHERE kind='etoro_live'` → 0; `SELECT enabled FROM connectors WHERE name='etoro'` → `false`, salvo attivazione fatta dall'utente;
  6. `docker compose exec -T api python -m smtrading.cli names audit` → elenco consegnato al controller, **nessun** `fix`;
  7. `git grep -l 'CANARY' -- ':!backend/tests' ':!docs' ':!web/e2e'` → nessun risultato; `git status --porcelain` pulito.
- [ ] **Step 4:** commit `docs: eToro, market closures, manual orders, multi-currency, Paperclip reader; e2e`
- [ ] **Step 5 (controller):**
  - nessuna attivazione;
  - si mostrano all'utente i nomi proposti (Task 1, Step 6) e il testo di §9 (Task 4, Step 5), e si applicano solo dopo il suo ok;
  - si annota nella memoria di progetto: «piano 9 chiuso, eToro reale costruito e spento».
