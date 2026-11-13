# SMTrading — Piano 8: Trading con denaro reale (costruito, non attivato)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** due adattatori live (`alpaca_live`, `t212_live`) con flusso di attivazione per collegamento (chiavi → test in sola lettura → prova a secco → «Attiva denaro reale» con TOTP), cancello di rete, protezioni automatiche e una notifica Telegram per ogni ordine reale. Tutto testato senza conti reali. **Alla chiusura del piano non esiste nessun collegamento live.**

**Architecture:**
- **Nucleo di protocollo condiviso:** la logica di `alpaca_paper.py` e `t212_demo.py` (payload, mapping, stati, `find_order`) passa in `_AlpacaProtocol` e `_T212Protocol`. Quattro classi concrete; l'URL è una `ClassVar` costante della classe concreta, mai letta dalla configurazione. Le due classi live stanno solo in `brokers/live/`.
- **Cancello live:** gli adattatori live accettano solo un `LiveClient` (httpx) costruito da `make_live_client`, il cui transport `LiveGateTransport` rilegge dal database, a ogni richiesta non-GET, stato del collegamento, kill switch e stato del portafoglio. Con il cancello chiuso la richiesta non apre nemmeno la connessione. In `dry_run` il `POST` viene intercettato e il payload restituito al chiamante.
- **Prima linea:** controlli pre-trade live (niente short, tetto di liquidità, fusibile di frequenza, kill switch senza eccezioni) e `BrokerSync._must_stop` che non reclama ordini di collegamenti non armati.
- **Sorveglianza:** il `LiveMonitor` del worker riconcilia posizioni e liquidità ogni 60 s e dopo ogni eseguito, controlla impronta del conto e salute; ogni anomalia porta il portafoglio in reduce-only con un avviso 🔴; si torna `active` solo con TOTP.
- **Telegram (piano 7):** le attività `live.*` vanno sempre in outbox subito (mai nel digest, mai `skipped`), silenziose nelle ore notturne; gli `order.*` dei portafogli reali vengono assorbiti dalle righe `live.order.*`.
- Il piano parte dal commit finale del piano 7, ramo `feat/plan-08-real-trading`.

**Tech Stack:** come i piani 1-7 (FastAPI, SQLAlchemy async, Alembic, httpx, respx, pyotp, mcp 2.x, React + TanStack Query + Vitest, Playwright). Nessuna dipendenza nuova.

**Spec:** `docs/superpowers/specs/2026-11-14-real-trading-design.md`, più le decisioni del controller R8.1-R8.3 in `.superpowers/sdd/2026-11-12-smtrading-06-connectors-brokers-extras/progress.md` (vincolanti, riportate qui sotto). Percorsi Python relativi a `backend/src/smtrading/` salvo indicazione.

## Global Constraints

- Valgono i vincoli dei piani 1-7: `Decimal` (mai float), `json_safe`, testi utente in italiano, `SMTError` con `ErrorCode`, audit admin con `write_audit`, nessun I/O di rete sotto lock di catena (R9), niente eseguiti locali su portafogli collegati (R10).
- **Decisioni vincolanti:**
  - nessun tetto globale al capitale; ordini reali autonomi (nessuna approvazione per ordine); una notifica Telegram per ogni ordine reale;
  - **R8.1** fusibile: al massimo `max_live_orders_per_hour` ordini reali inviati per portafoglio in 3600 s scorrevoli, **default 30**, modificabile dall'admin nel profilo di rischio (1-1000); superato → reduce-only + 🔴;
  - **R8.2** kill switch acceso: blocca **ogni** invio live (anche le chiusure); passano solo le cancellazioni (`DELETE`) e gli ordini a mercato reduce-only creati da «Chiudi tutte le posizioni reali» (admin, TOTP), marcati `orders.live_flatten = true`;
  - **R8.3** niente «base esterna»: un collegamento live richiede un conto dedicato; test, attivazione e collegamento rifiutano un conto con posizioni o ordini aperti;
  - l'adattatore `alpaca_live` si costruisce anche se l'apertura del conto per residenti italiani non è confermata; **cripto live disattivata di default** (`crypto_enabled=false`), attivabile solo se l'ultimo snapshot del conto riporta la cripto attiva.
- **URL live:** `AlpacaLiveAdapter.BASE_URL = "https://api.alpaca.markets"`, `T212LiveAdapter.BASE_URL = "https://live.trading212.com"`, solo come `ClassVar` in `brokers/live/`. Nessuna configurazione, variabile d'ambiente, fixture o test li ripete: chi ne ha bisogno importa `LIVE_HOSTS` da `smtrading.brokers.live`. Un test di guardia (Task 4) scandisce tutti i file tracciati da git, esclusi `docs/**` e `backend/src/smtrading/brokers/live/**`, e fallisce se trova `live.trading212.com` o `(?<![\w-])api\.alpaca\.markets`.
- **Costruttori:** ogni adattatore rifiuta con `FORBIDDEN` un `config["base_url"]` diverso dalla propria costante; le classi paper/demo rifiutano esplicitamente gli host di `LIVE_HOSTS` (messaggio «host di produzione non ammesso per un adattatore paper»); le classi live rifiutano un `http` che non sia un `LiveClient`.
- **Cancello live:** `GET`/`HEAD` passano sempre; `DELETE` passa in ogni `live_state`; `POST`/`PUT`/`PATCH` solo con collegamento `armed`, portafoglio collegato non `locked` e kill switch spento (o ordine `live_flatten`); in `dry_run` il `POST` di un ordine solleva `LiveDryRun`, ogni altro non-GET solleva `LiveGateClosed`. Lo stato si rilegge a **ogni** richiesta, in una sessione breve.
- **Stati:** `live_state ∈ {configured, dry_run, armed}`, `NULL` per paper/demo. Chiavi sostituite o impronta diversa al test → `configured` (nuovo TOTP). Disattivazione: un clic, nessun TOTP.
- **Collegamento portafoglio ↔ live:** consentito in `dry_run` o `armed` (per la prova a secco serve un portafoglio collegato; risoluzione prudente dello spec, che chiedeva `armed`), sempre con TOTP; 1 collegamento live ↔ 1 portafoglio.
- **Tetto di liquidità:** acquisto ammesso se `stima + commissioni stimate + LIVE_CASH_MARGIN_PCT (2%) × stima ≤ min(liquidità disponibile del portafoglio, liquidità al broker convertita nella valuta del portafoglio)`; snapshot del broker valido se letto da ≤ `SNAPSHOT_MAX_AGE_S = 60` s; per Trading 212 al massimo una lettura del conto ogni 5 s.
- **Riconciliazione live:** ogni `LIVE_RECONCILE_INTERVAL_S = 60` s e dopo ogni eseguito; tolleranza liquidità `max(0,50 € convertiti, 0,1%)`; non corregge mai nulla.
- **TOTP:** obbligatorio per attivare, collegare, riprendere («Ho verificato, riprendi») e «Chiudi tutte le posizioni reali». Si riusa `admin.auth._check_totp` (anti-replay). Admin senza TOTP → `FORBIDDEN` «Attiva prima l'autenticazione a due fattori».
- **Segreti:** chiavi live cifrate con `encrypt_json(..., aad=b"broker_live")`; mai restituite (solo `••••` + ultime 4 della key ID, ID conto mascherato `••••1234`); mai in log, errori, audit, outbox, `broker_raw`.
- **Test senza rete:** respx con `assert_all_mocked=True` e `assert_all_called=True` per ogni test che tocca un adattatore live; fixture di sessione autouse che fa fallire `socket.getaddrinfo`/`socket.create_connection` verso `LIVE_HOSTS`. Nessuna chiave reale nel repo: nei test solo chiavi «canarino» (`CANARYKEYID0001`, `CANARYSECRET-do-not-use`).
- **Nessun task collega un conto reale**, crea un collegamento live nel database dello stack principale o salva chiavi live. Il controller non lo fa nemmeno alla fine.
- **Migrazione:** il numero libero successivo a quella del piano 7 (attesa `0016_real_trading.py` se il piano 7 ha aggiunto solo `0015_telegram.py`). Qui sotto: `00MM_real_trading.py`.
- **Documentazione delle API esterne:** prima dei Task 2 e 4 l'implementer verifica su `docs.alpaca.markets` e `docs.trading212.com` gli endpoint del conto (`GET /v2/account`; `GET /api/v0/equity/account/cash` e `/info` o i loro successori) e le intestazioni `x-ratelimit-*`, e annota gli URL nel report.

## Review Focus

1. **Esito sconosciuto su `t212_live`** (timeout o 5xx dopo il `POST`) → nessun reinvio automatico, ricerca con `find_order`, al `_give_up` il portafoglio va in reduce-only con 🔴 «esito sconosciuto»; un nuovo ordine dell'agente sullo stesso simbolo viene accettato solo se riduce. Test in Task 6.
2. **Stato che cambia tra il controllo di BrokerSync e l'invio** (kill switch acceso, disattivazione o `locked` dopo `_must_stop`) → il cancello rilegge lo stato e il `POST` non raggiunge il transport fake; l'ordine diventa `rejected` con `KILL_SWITCH_ACTIVE` o `LIVE_NOT_ARMED`, mai `unknown`. Test in Task 3 (transport) e Task 6 (sync).
3. **Conto diverso dietro le stesse credenziali o chiavi sostituite** → `configured`, `account_fingerprint` aggiornato solo dopo un nuovo test, portafoglio collegato in reduce-only con 🔴 «conto cambiato»; riattivazione solo con nuovo TOTP. Test in Task 5 e Task 7.
4. **Telegram non configurato, in pausa o irraggiungibile con un collegamento armato** → gli ordini reali continuano, ogni `live.order.*` finisce comunque in `telegram_outbox` (mai `skipped`, mai nel digest), di notte parte subito con `disable_notification=true`; la UI mostra l'avviso fisso. Test in Task 3.
5. **Reduce-only automatico live tolto da un percorso esistente** (azzeramento della perdita giornaliera a fine giornata, `PATCH` dello stato del portafoglio dall'admin, unarchive) → rifiutato: con `reduce_only_reason` che inizia per `live_` si torna `active` solo con `resume_live_portfolio` (TOTP). Test in Task 3.

---

## File Structure

```
backend/src/smtrading/
  common/crypto.py            (+ aad)          common/errors.py  (+ 3 codici)
  common/logsetup.py          (+ segreti registrati, httpx muto per LIVE_HOSTS)
  brokers/base.py             (+ live, AccountSnapshot, get_account, BrokerOrder.fees)
  brokers/alpaca_core.py      _AlpacaProtocol (estratto da alpaca_paper.py)
  brokers/t212_core.py        _T212Protocol (estratto da t212_demo.py)
  brokers/alpaca_paper.py, t212_demo.py     solo classi concrete
  brokers/registry.py         (+ brokers.live)
  brokers/service.py          (rifiuta kind live nel CRUD paper; unlink live)
  brokers/live/__init__.py    LIVE_HOSTS, LIVE_KINDS
  brokers/live/models.py      LiveState, LiveAudit
  brokers/live/audit.py       record_live_audit
  brokers/live/gate.py        LiveClient, LiveGateTransport, make_live_client, LiveGateClosed, LiveDryRun
  brokers/live/protect.py     trip_reduce_only, live_alert, record_live_order_activity, render_live_activity
  brokers/live/alpaca_live.py AlpacaLiveAdapter
  brokers/live/t212_live.py   T212LiveAdapter
  brokers/live/fake.py        FakeLiveBroker (MockTransport, solo con live_fake_broker)
  brokers/live/service.py     chiavi, test, prova a secco, arm, disarm, link, resume, flatten
  brokers/live/reconcile.py   reconcile_live
  brokers/live/monitor.py     LiveMonitor
  risk/live_checks.py         live_pre_trade, cash cap, fusibile
  portfolios/mode.py          reale + live_state
  admin/routes_live.py        /api/live-links/*, /api/portfolios/{id}/live-*, /api/live-audit
  devtools/live_e2e_seed.py   solo e2e, rifiuta senza live_fake_broker
backend/migrations/versions/00MM_real_trading.py
web/src/components/LiveLinkWizard.tsx, LiveArmDialog.tsx, LiveOrderConfirm.tsx
web/src/pages/LiveAudit.tsx   web/src/api/hooks/live.ts
backend/tests/unit/test_live_hosts_guard.py, test_live_checks.py, test_live_render.py
backend/tests/integration/test_brokers_protocol.py, test_live_gate.py, test_brokers_live.py,
  test_live_service.py, test_live_sync.py, test_live_monitor.py, test_admin_live.py
web/e2e/live.spec.ts
```

## Parallelismo e proprietà dei file

| Ondata | Task | Note |
|---|---|---|
| A | 1 | schema, codici, crypto; prerequisito di tutto |
| B | 2 ∥ 3 | 2 possiede `brokers/{base,alpaca_core,t212_core,alpaca_paper,t212_demo}.py` e i test paper; 3 possiede `brokers/live/{gate,protect}.py`, `telegram/planner.py`, `portfolios/service.py` (guardia reduce-only), `risk/daily_loss.py` (guardia) |
| C | 4 | dopo 2 e 3: `brokers/live/{__init__,alpaca_live,t212_live,fake}.py`, `brokers/registry.py`, `common/logsetup.py`, `tests/conftest.py`, guardia degli host |
| D | 5 ∥ 6 ∥ 7 | 5: `brokers/live/service.py`, `brokers/service.py`, `portfolios/mode.py`; 6: `risk/{live_checks,checks}.py`, `engine/service.py`, `engine/execution.py`, `brokers/sync.py`; 7: `brokers/live/{reconcile,monitor}.py`, `jobs/scheduler.py`, `worker/main.py` |
| E | 8 | API admin, MCP, CLI (dopo 5-7) |
| F | 9 | solo `web/` (contratto del Task 8) |
| G | 10 | e2e, documentazione, verifica finale |

---

### Task 1: Schema, codici d'errore, cifratura con contesto

**Files:**
- Create: `brokers/live/__init__.py` (vuoto per ora), `brokers/live/models.py`, `brokers/live/audit.py`, `migrations/versions/00MM_real_trading.py`
- Modify: `brokers/models.py`, `risk/models.py`, `engine/models.py`, `engine/service.py` (solo il campo `OrderRequest.live_flatten`), `common/errors.py`, `common/crypto.py`, `common/settings.py` (`live_fake_broker: bool = False`), `models.py` (registro)
- Test: `tests/integration/test_migrations.py` (drift), `tests/integration/test_live_audit.py`, `tests/unit/test_crypto.py`

**Interfaces — Produces:**
- `class LiveState(StrEnum)`: `configured, dry_run, armed`.
- `BrokerLink` nuove colonne: `live_state String(16)|None` (CHECK sui 3 valori o NULL); `armed_at`, `disarmed_at` timestamptz|None; `armed_by String(128)|None`; `disarmed_reason String(200)|None`; `account_fingerprint String(64)|None`; `last_account_snapshot JSONB|None`; `last_snapshot_at` timestamptz|None; `last_test_at` timestamptz|None; `health_failures int default 0`.
- `RiskProfile.max_live_orders_per_hour: int` not null, `server_default="30"`, CHECK 1-1000.
- `Order.live_flatten: bool` default false, `server_default="false"`; `OrderRequest.live_flatten: bool = False` propagato all'`Order` creato.
- `LiveAudit` (`live_audit`): `id BIGINT identity; ts timestamptz server_default clock_timestamp(); link_id UUID FK broker_links; portfolio_id UUID FK|None; order_id UUID FK orders|None; actor String(128)` (email admin, `system`, `agent:<uuid>`); `action String(32)` CHECK su `keys_saved, test_ok, test_failed, dry_run_on, dry_run_off, armed, disarmed, portfolio_linked, portfolio_unlinked, reduce_only_auto, resumed, kill_switch, flatten, order_sent`; `ip String(64)|None; user_agent String(300)|None; details JSONB default {}`. Indici `(link_id, id)`, `(portfolio_id, id)`. **Trigger** `live_audit_append_only` che solleva su `UPDATE` e `DELETE`.
- `async def record_live_audit(session, *, action: str, link_id: UUID, actor: str, portfolio_id: UUID|None = None, order_id: UUID|None = None, ip: str|None = None, user_agent: str|None = None, details: dict|None = None) -> LiveAudit` — `details` passa da `redact` (`admin/common.py`) e da `json_safe`.
- `ErrorCode.LIVE_NOT_ARMED`, `LIVE_DRY_RUN`, `LIVE_CASH_LIMIT`.
- `encrypt_json(data, master_key, *, aad: bytes|None = None)`, `decrypt_json(blob, master_key, *, aad: bytes|None = None)` — `aad` passato ad AESGCM; i blob esistenti (aad None) restano leggibili.

- [ ] **Step 1: test falliti:**
  - `test_migration_matches_models` (drift esistente);
  - `test_live_audit_is_append_only` — insert OK; `UPDATE` e `DELETE` → `DBAPIError`;
  - `test_live_audit_details_redacted` — `details={"secret_key": "CANARYSECRET-do-not-use"}` → salvato `"••••"`;
  - `test_risk_profile_live_fuse_default_30`, `test_order_live_flatten_default_false`;
  - `test_aad_roundtrip_and_mismatch` — cifrato con `aad=b"broker_live"`, decifrato senza aad → `SMTError(FORBIDDEN)`; blob senza aad decifrato come prima.
- [ ] **Step 2:** `cd backend && uv run pytest tests/integration/test_migrations.py tests/integration/test_live_audit.py tests/unit/test_crypto.py -q` → FAIL.
- [ ] **Step 3:** modelli, migrazione (upgrade/downgrade, trigger incluso), codici, crypto.
- [ ] **Step 4:** PASS; `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` pulito; `uv run pytest -q` verde.
- [ ] **Step 5:** commit `feat(live): schema for live link state, live audit, live fuse, flatten flag`

### Task 2: Nucleo di protocollo condiviso e snapshot del conto

**Files:**
- Create: `brokers/alpaca_core.py`, `brokers/t212_core.py`
- Modify: `brokers/base.py`, `brokers/alpaca_paper.py`, `brokers/t212_demo.py`
- Test: `tests/integration/test_brokers_protocol.py` (nuovo, parametrizzato), `tests/integration/test_brokers_alpaca.py`, `tests/integration/test_brokers_t212.py` (invariati salvo import)

**Interfaces — Consumes:** nulla oltre al piano 6.

**Interfaces — Produces:**
- `BrokerAdapter.live: ClassVar[bool] = False`; `BrokerAdapter.BASE_URL: ClassVar[str]`. Docstring: «Paper/demo adapters never talk to real-money endpoints. Live adapters (brokers/live) talk to them only through the live gate.»
- `BrokerOrder.fees: Decimal|None = None` (commissioni riportate dal broker nella valuta del conto; Alpaca: commissioni regolamentari se presenti nella risposta; T212: commissione FX se presente).
- `@dataclass(frozen=True) AccountSnapshot: account_id: str; account_type: str|None; currency: str; cash_available: Decimal; positions_count: int; open_orders_count: int; crypto_enabled: bool|None; order_types: frozenset[OrderType]|None; raw: dict`.
- `async def BrokerAdapter.get_account(self) -> AccountSnapshot` — solo `GET`. Alpaca: `/v2/account` (`id`, `currency`, `cash` o `buying_power` con leva 1 = `cash`, `crypto_status == "ACTIVE"`) + conteggi da `/v2/positions` e `/v2/orders?status=open`. T212: endpoint del conto (`id`, `currencyCode`, `free`) + conteggi di posizioni e ordini.
- `class _AlpacaProtocol(BrokerAdapter)` e `class _T212Protocol(BrokerAdapter)` — tutta la logica attuale; usano `self.BASE_URL`; ogni richiesta d'ordine passa `extensions={"smt_order_id": str(order.id)}` a `http.request`. `_T212Protocol` legge `x-ratelimit-remaining`/`x-ratelimit-reset` e, a `remaining == 0`, attende fino al reset prima della richiesta successiva (sleep iniettabile); un 429 solleva `RuntimeError("limite di frequenza Trading 212")` (ritentabile, non un rifiuto).
- `AlpacaPaperAdapter(_AlpacaProtocol)` con `BASE_URL = PAPER_URL`, `T212DemoAdapter(_T212Protocol)` con `BASE_URL = DEMO_URL`; costruttori: `base_url` ≠ costante → `FORBIDDEN`. (Il rifiuto esplicito degli host live arriva nel Task 4, quando esiste `LIVE_HOSTS`.)
- Comportamento verso l'esterno invariato: le suite del piano 6 passano senza modifiche alle asserzioni.

- [ ] **Step 1: test falliti** (`test_brokers_protocol.py`, parametrizzato oggi sulle 2 classi paper, il Task 4 aggiunge le 2 live): `test_place_payload_shape`, `test_status_mapping`, `test_find_order_contract`, `test_get_account_snapshot` (fixture JSON nuove in `tests/fixtures/brokers/`), `test_order_requests_carry_order_id_extension`, `test_t212_ratelimit_waits_for_reset`, `test_t212_429_is_retryable_not_rejected`, `test_fees_parsed_when_present`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_brokers_protocol.py -q` → FAIL.
- [ ] **Step 3:** estrazione del nucleo, `get_account`, `fees`.
- [ ] **Step 4:** PASS, poi `uv run pytest tests/integration/test_brokers_alpaca.py tests/integration/test_brokers_t212.py tests/integration/test_brokers_sync.py -q` verdi senza modifiche alle asserzioni.
- [ ] **Step 5:** commit `refactor(brokers): shared Alpaca/T212 protocol cores, account snapshot, broker fees`

### Task 3: Cancello live, protezioni comuni e Telegram per gli ordini reali

**Files:**
- Create: `brokers/live/gate.py`, `brokers/live/protect.py`
- Modify: `telegram/planner.py` (regole `live.*`), `portfolios/service.py` e ogni percorso che rimette `active` (guardia `live_`), `risk/daily_loss.py` (guardia)
- Test: `tests/integration/test_live_gate.py`, `tests/unit/test_live_render.py`, `tests/integration/test_live_protect.py`

**Interfaces — Consumes:** Task 1 (`LiveState`, `Order.live_flatten`, `record_live_audit`); piano 7 (`record_activity`, `ActivityDraft`, `ActivityLevel`, `enqueue`, `TelegramPlanner`); `risk.settings.is_kill_switch_on`.

**Interfaces — Produces:**
- `class LiveGateClosed(Exception)`: `code: ErrorCode` (`LIVE_NOT_ARMED`, `KILL_SWITCH_ACTIVE`, `PORTFOLIO_LOCKED`), `message: str` italiano.
- `class LiveDryRun(Exception)`: `payload: dict` (corpo JSON della richiesta, senza intestazioni), `method`, `path`.
- `class LiveGateTransport(httpx.AsyncBaseTransport)(inner: httpx.AsyncBaseTransport, sessionmaker, link_id: UUID)` — `handle_async_request`:
  - `GET`/`HEAD` → `inner`;
  - `DELETE` → `inner` in ogni `live_state` non nullo;
  - altro: legge in una sessione breve `BrokerLink.live_state`, il portafoglio collegato (`Portfolio.broker_link_id == link_id`), `is_kill_switch_on`, e l'ordine di `request.extensions["smt_order_id"]` se presente;
    - `configured` o link mancante → `LiveGateClosed(LIVE_NOT_ARMED, "denaro reale non attivo su questo collegamento")`;
    - `dry_run` → `LiveDryRun(payload)` se `smt_order_id` presente, altrimenti `LiveGateClosed(LIVE_NOT_ARMED)`;
    - `armed` e portafoglio `locked` → `LiveGateClosed(PORTFOLIO_LOCKED)`;
    - `armed` e kill switch acceso → passa solo se l'ordine ha `live_flatten`, `reduce_only` e `type == market`; altrimenti `LiveGateClosed(KILL_SWITCH_ACTIVE)`;
    - altrimenti `inner`.
- `class LiveClient(httpx.AsyncClient)` — marcatore; `def make_live_client(sessionmaker, link_id: UUID, *, inner: httpx.AsyncBaseTransport|None = None, timeout: float = 10.0) -> LiveClient` (`inner` default `httpx.AsyncHTTPTransport()`, così respx intercetta sotto il cancello).
- `async def trip_reduce_only(session, portfolio_id: UUID, *, cause: str, details: dict, actor: str = "system") -> bool` — `cause ∈ {reconcile_diff, order_unknown, fingerprint_changed, cash_shortfall, fuse, health}`; se `active` → `reduce_only`, `reduce_only_reason=f"live_{cause}"`, `reduce_only_until=None`; scrive `record_live_audit(action="reduce_only_auto")` e `live_alert`; ritorna False se era già reduce-only/locked (avviso comunque, una volta per causa per ora).
- `async def live_alert(session, *, kind: str, summary: str, portfolio_id: UUID|None, link_id: UUID, data: dict) -> None` — `record_activity(ActivityDraft(type=f"live.{kind}", level=critical, direct=True, ...))`.
- `async def record_live_order_activity(session, order: Order, phase: Literal["sent","filled","partial","rejected","canceled","dry_run"], *, bo: BrokerOrder|None = None, message: str|None = None) -> None` — `type=f"live.order.{phase}"`, livello `warning` per `rejected`, `info` altrimenti, `direct=True`, `data` con portafoglio, simbolo, lato, qty/notional, tipo, agente, prezzo, valore, commissioni, messaggio del broker.
- `def render_live_activity(a: Activity, names: Names, tz: str) -> str` — prima riga esatta per fase: `🔴 REALE · inviato`, `🔴 REALE · eseguito`, `🔴 REALE · parziale`, `🔴 REALE · rifiutato`, `🔴 REALE · annullato`, `🧪 REALE (prova)`; poi portafoglio, `buy 0,5 AAPL · mercato`, agente, ora; per gli eseguiti prezzo, valore, commissioni.
- `TelegramPlanner` (piano 7), due regole prima di quelle esistenti:
  - `type` che inizia per `live.` → sempre `enqueue` subito con `render_live_activity` (anche se `not is_ready(cfg)` e anche di notte; la pausa produce righe `paused`), `telegram="queued"`;
  - `type` che inizia per `order.` e portafoglio collegato a un `kind` in `LIVE_KINDS` (lettura del link nel lotto) → `skipped` (lo copre la riga `live.order.*`).
- Guardia: qualsiasi passaggio `reduce_only → active` con `reduce_only_reason` che inizia per `live_` solleva `SMTError(FORBIDDEN, "riprendi il portafoglio reale con «Ho verificato, riprendi» (TOTP)")`, tranne `brokers.live.service.resume_live_portfolio` (Task 5), che passa `allow_live=True` alla funzione di stato.

- [ ] **Step 1: test falliti** (`test_live_gate.py`, transport interno = `httpx.MockTransport` che conta le chiamate):
  - matrice `test_gate_matrix` parametrizzata su `live_state ∈ {configured, dry_run, armed}` × kill switch × `locked` × metodo `GET|POST|DELETE` con risultato atteso (passa, `LiveDryRun`, `LiveGateClosed(codice)`) e contatore interno a 0 quando chiuso;
  - `test_flatten_order_bypasses_kill_switch_only_when_reduce_only_market`;
  - `test_state_reread_per_request` — primo `POST` passa, si accende il kill switch, secondo `POST` → `LiveGateClosed` senza toccare il transport (Review Focus 2);
  - `test_dry_run_returns_payload_without_headers`.
- [ ] **Step 2: test falliti** (`test_live_render.py` puro e `test_live_protect.py`):
  - `test_render_first_lines_per_phase`, `test_render_italian_decimals`;
  - `test_trip_reduce_only_sets_live_reason_and_alert`;
  - `test_live_activity_enqueued_when_telegram_not_ready_and_at_night` — config non pronta → riga `pending` in outbox; 23:30 Rome → nessun digest (Review Focus 4);
  - `test_order_events_of_live_portfolio_skipped`;
  - `test_live_reduce_only_not_cleared_by_daily_reset_or_patch` (Review Focus 5).
- [ ] **Step 3:** `uv run pytest tests/integration/test_live_gate.py tests/unit/test_live_render.py tests/integration/test_live_protect.py -q` → FAIL.
- [ ] **Step 4:** implementazione → PASS; `uv run pytest tests/integration/test_telegram_worker.py tests/integration/test_daily_loss.py tests/integration/test_portfolios.py -q` verdi.
- [ ] **Step 5:** commit `feat(live): live gate transport, auto reduce-only, live order telegram activities`

### Task 4: Adattatori live, registro e guardie di rete

**Files:**
- Create: `brokers/live/alpaca_live.py`, `brokers/live/t212_live.py`, `brokers/live/fake.py`; `tests/unit/test_live_hosts_guard.py`, `tests/integration/test_brokers_live.py`
- Modify: `brokers/live/__init__.py`, `brokers/registry.py`, `brokers/alpaca_paper.py`/`t212_demo.py` (rifiuto esplicito di `LIVE_HOSTS`), `common/logsetup.py`, `tests/conftest.py`, `tests/integration/test_brokers_protocol.py` (parametrizzazione + 2 classi)

**Interfaces — Consumes:** Task 2 (`_AlpacaProtocol`, `_T212Protocol`, `AccountSnapshot`), Task 3 (`LiveClient`, `make_live_client`).

**Interfaces — Produces:**
- `AlpacaLiveAdapter(_AlpacaProtocol)`: `kind = "alpaca_live"`, `live = True`, `BASE_URL = "https://api.alpaca.markets"`; config `key_id`, `secret_key`, `crypto_enabled: bool = False`. `supports`: cripto solo con `crypto_enabled`; `notional` solo `market` con `tif=day`; frazioni solo `tif=day`; niente ordini in orario esteso con frazioni.
- `T212LiveAdapter(_T212Protocol)`: `kind = "t212_live"`, `live = True`, `idempotent_place = False`, `BASE_URL = "https://live.trading212.com"`; config `api_key`, `api_secret`, `order_types: list[str]` default `["market","limit","stop","stop_limit"]`; `supports` limita ai tipi configurati; solo quantità.
- Entrambi: `__init__` → `FORBIDDEN` se `http` non è `LiveClient` («gli adattatori live richiedono il cancello») o se `config.get("base_url")` è presente e diverso da `BASE_URL`.
- `brokers/live/__init__.py`: `LIVE_KINDS = frozenset({"alpaca_live", "t212_live"})`; `LIVE_HOSTS: frozenset[str]` calcolato con `urlsplit(cls.BASE_URL).hostname` dalle due classi (import pigro per evitare cicli).
- `registry._load()` importa anche `smtrading.brokers.live.alpaca_live` e `t212_live`.
- Classi paper/demo: `base_url` con host in `LIVE_HOSTS` → `FORBIDDEN` «host di produzione non ammesso per un adattatore paper».
- `class FakeLiveBroker` (`fake.py`): `transport() -> httpx.MockTransport` che risponde agli endpoint di conto, posizioni, ordini (stato in memoria, conto vuoto, liquidità `1000.00`, valuta EUR per T212 e USD per Alpaca); usato da `make_live_client` al posto di `AsyncHTTPTransport` **solo** se `get_settings().live_fake_broker` è vero (e2e). Con il fake il cancello resta attivo.
- `common/logsetup.py`: `register_secret(value: str) -> None` (insieme dei valori da redigere in `redact_log_text`, minimo 8 caratteri); filtro che scarta i record dei logger `httpx` e `httpcore` il cui messaggio contiene un host di `LIVE_HOSTS`.
- `tests/conftest.py`: fixture di sessione autouse `_no_live_network` che avvolge `socket.getaddrinfo` e `socket.create_connection` e solleva `RuntimeError("tentata connessione a un host live nei test")` per gli host in `LIVE_HOSTS`.
- `tests/unit/test_live_hosts_guard.py`: legge `git ls-files` dalla radice del repo, esclude `docs/**`, `backend/src/smtrading/brokers/live/**` e i binari; cerca i pattern costruiti da `LIVE_HOSTS` (Alpaca con lookbehind `(?<![\w-])` per non colpire `paper-api.`); nessuna corrispondenza ammessa.

- [ ] **Step 1: test falliti:**
  - `test_live_hosts_only_in_brokers_live` (scansione) e `test_guard_detects_a_planted_host` (file temporaneo in una copia dell'elenco: la funzione di scansione lo segnala);
  - `test_socket_guard_blocks_live_host` — `socket.getaddrinfo(next(iter(LIVE_HOSTS)), 443)` → `RuntimeError`;
  - `test_brokers_protocol.py` parametrizzato sulle 4 classi (le live con `make_live_client` su un link `armed` del db di test e respx con `assert_all_mocked`/`assert_all_called`);
  - `test_live_rejects_plain_httpx_client`, `test_live_rejects_other_base_url`, `test_paper_rejects_live_hosts`;
  - `test_alpaca_live_crypto_off_by_default`, `test_alpaca_live_notional_market_day_only`, `test_t212_live_types_from_config`;
  - `test_httpx_logs_for_live_hosts_dropped`, `test_registered_secret_redacted`;
  - `test_fake_broker_only_with_setting`.
- [ ] **Step 2:** `uv run pytest tests/unit/test_live_hosts_guard.py tests/integration/test_brokers_live.py tests/integration/test_brokers_protocol.py -q` → FAIL.
- [ ] **Step 3:** implementazione.
- [ ] **Step 4:** PASS; `uv run pytest -q` verde (nessun test esistente apre host live).
- [ ] **Step 5:** commit `feat(live): Alpaca and Trading 212 live adapters behind the gate, live-host guards`

### Task 5: Servizio di attivazione e stato del portafoglio reale

**Files:**
- Create: `brokers/live/service.py`
- Modify: `brokers/service.py` (CRUD paper rifiuta `LIVE_KINDS`; `get_adapter` per i kind live usa `make_live_client`; unlink live), `portfolios/mode.py`
- Test: `tests/integration/test_live_service.py`

**Interfaces — Consumes:** Task 1, 3 (`make_live_client`, `live_alert`, `trip_reduce_only`), 4 (adattatori, `LIVE_KINDS`, `register_secret`); `admin.auth._check_totp`; piano 7 (`load_config`, `is_ready`, `TelegramOutbox`); `engine.service.place_order`, `set_kill_switch_and_cancel`.

**Interfaces — Produces** (tutte le funzioni scrivono `record_live_audit` con `actor`, `ip`, `user_agent` da un `@dataclass Actor: email: str; ip: str|None; user_agent: str|None`):
- `async def save_live_keys(session, *, name: str, kind: str, config: dict, actor: Actor, link_id: UUID|None = None) -> BrokerLink` — nuovo o sostituzione; cifra con `aad=b"broker_live"`; stato `configured`, `armed_at=None`; con sostituzione su `armed`/`dry_run` prima `disarm_live_link(reason="chiavi sostituite")`; `register_secret` dei valori; azione `keys_saved` con `details={"key_last4"}`.
- `async def test_live_link(sessionmaker, link_id, *, actor: Actor) -> dict` — solo `GET` (`get_account`); I/O fuori transazione; salva `last_account_snapshot` (senza `raw`), `last_snapshot_at`, `last_test_at`; `fingerprint = sha256(f"{kind}:{account_id}")`; impronta diversa da quella salvata → `configured` + `trip_reduce_only(cause="fingerprint_changed")` sul portafoglio collegato + `live_alert("account_changed")` (Review Focus 3); azione `test_ok`/`test_failed`. Ritorna `{account_masked, account_type, currency, cash_available, positions_count, open_orders_count, crypto_enabled, order_types, empty: bool}`.
- `async def set_dry_run(session, link_id, on: bool, *, actor) -> BrokerLink` — `configured → dry_run` richiede un test riuscito con conto vuoto; `dry_run → configured` con `on=False`; da `armed` → `INVALID_ORDER`.
- `LIVE_TEST_MAX_AGE_S = 900`. `async def arm_live_link(session, redis, link_id, *, admin: AdminUser, totp: str, confirm_name: str, accept_risks: bool, dedicated_account: bool, limits_set: bool, actor: Actor) -> BrokerLink` — in ordine, ciascun errore con `details.field`:
  1. `admin.totp_enabled` falso → `FORBIDDEN` «Attiva prima l'autenticazione a due fattori» (`field=totp`);
  2. le tre spunte vere, `confirm_name == link.name` (`field=confirm_name`);
  3. stato `configured|dry_run`;
  4. ultimo test riuscito da ≤ 900 s, conto vuoto, impronta invariata (`field=test`);
  5. Telegram: `is_ready(cfg)`, non in pausa, una riga `TelegramOutbox(category="test", status="sent", sent_at >= now − 24 h)` (`field=telegram`);
  6. se c'è un portafoglio collegato, `check_live_portfolio` passa;
  7. `_check_totp(redis, admin, totp)`;
  → `armed`, `armed_at`, `armed_by`; `live_alert("armed")`; azione `armed`.
- `async def disarm_live_link(session, link_id, *, actor: Actor|str, reason: str) -> int` — nessun TOTP; → `configured`, `disarmed_at`, `disarmed_reason`; ordini aperti già al broker → cancellazione richiesta (la esegue BrokerSync, il `DELETE` passa); ordini non inviati (`to_send`, nessun `broker_sent_at`) → `rejected` con `LIVE_NOT_ARMED` «denaro reale disattivato»; `live_alert("disarmed")`; ritorna il numero di ordini toccati.
- `async def check_live_portfolio(session, portfolio: Portfolio, link: BrokerLink) -> None` — `INVALID_ORDER` con messaggio e `field`: profilo con `max_order_notional_eur` e uno tra `max_daily_loss_pct`/`_eur` valorizzati; `short_allowed` falso; `leverage == 1`; T212: `base_currency == snapshot.currency`; liquidità disponibile ≤ liquidità del broker convertita; nessun altro portafoglio sul link; snapshot vuoto (R8.3, «Il conto non è dedicato: chiudi posizioni e ordini dall'app del broker»).
- `async def link_live_portfolio(session, redis, portfolio_id, link_id, *, admin, totp, actor) -> Portfolio` — stato `dry_run|armed`, `check_live_portfolio`, regole di `link_portfolio` (niente ordini aperti, niente posizioni), TOTP; azione `portfolio_linked`.
- Unlink da un link live (`brokers.service.link_portfolio(..., None)`): rifiutato con posizioni aperte «Chiudi prima le posizioni reali»; azione `portfolio_unlinked`.
- `async def resume_live_portfolio(session, redis, portfolio_id, *, admin, totp, actor) -> Portfolio` — solo con `reduce_only_reason` `live_*`; TOTP; → `active`; azione `resumed`.
- `async def flatten_live_portfolio(session, redis, portfolio_id, *, admin, totp, actor) -> list[UUID]` — TOTP; per ogni posizione un `place_order(OrderRequest(type=market, reduce_only=True, live_flatten=True, source="admin", ...))`; azione `flatten`; `live_alert("flatten")`.
- `portfolios/mode.py`: `portfolio_mode(portfolio, link_kind: str|None = None) -> PortfolioMode` (`reale` se `link_kind in LIVE_KINDS`) e `def live_state_of(link: BrokerLink|None) -> str|None`; i chiamanti esistenti passano il `kind` del link (join già presente nei DTO; i DTO li aggiorna il Task 8).

- [ ] **Step 1: test falliti** (respx sul `FakeLiveBroker` o su fixture JSON; TOTP con `pyotp.TOTP(secret).now()`):
  - `test_keys_saved_encrypted_with_aad_and_never_returned`;
  - `test_test_link_is_get_only` (respx: solo route `GET`; nessun `POST` registrato);
  - `test_arm_requires_totp_enabled`, `test_arm_requires_checkboxes_and_name`, `test_arm_requires_fresh_empty_test`, `test_arm_requires_telegram_test_last_24h`, `test_arm_totp_replay_rejected`, `test_arm_ok_audited_and_alerted`;
  - `test_replacing_keys_returns_configured_and_needs_new_totp`;
  - `test_fingerprint_change_disarms_and_reduce_only` (Review Focus 3);
  - `test_account_not_empty_refused` (R8.3);
  - `test_link_checks_profile_short_leverage_currency_cash_one_to_one`;
  - `test_link_allowed_in_dry_run`;
  - `test_disarm_one_click_cancels_sent_rejects_unsent` (nessun `POST` al fake; cancellazioni richieste);
  - `test_resume_requires_totp_and_live_reason`;
  - `test_flatten_creates_reduce_only_market_flagged_orders`;
  - `test_mode_reale_for_live_kinds`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_live_service.py -q` → FAIL.
- [ ] **Step 3:** implementazione.
- [ ] **Step 4:** PASS; `uv run pytest tests/integration/test_admin_brokers.py tests/integration/test_portfolios.py -q` verdi.
- [ ] **Step 5:** commit `feat(live): activation flow (keys, read-only test, dry run, TOTP arm, one-click disarm), live portfolio link`

### Task 6: Controlli pre-trade live e percorso d'invio in BrokerSync

**Files:**
- Create: `risk/live_checks.py`
- Modify: `risk/checks.py` (chiamata a `live_pre_trade`), `engine/service.py` (passaggio di `live_flatten`), `engine/execution.py` (`fee_override`), `brokers/sync.py`
- Test: `tests/unit/test_live_checks.py`, `tests/integration/test_live_sync.py`

**Interfaces — Consumes:** Task 1, 3 (`LiveGateClosed`, `LiveDryRun`, `trip_reduce_only`, `record_live_order_activity`, `record_live_audit`), 4, 5 (`get_adapter` con cancello).

**Interfaces — Produces:**
- `LIVE_CASH_MARGIN_PCT = Decimal("2")`, `SNAPSHOT_MAX_AGE_S = 60`.
- `def live_cash_needed(estimate: Decimal, fees: Decimal) -> Decimal` = `estimate + fees + estimate × 2 / 100`.
- `async def live_pre_trade(session, *, portfolio, link: BrokerLink, instrument, side, qty, notional, est_value: Decimal, est_fees: Decimal, reduce_only: bool, live_flatten: bool, now: datetime) -> None` — chiamata da `pre_trade_check` solo se il portafoglio è collegato a un kind live, **prima** dei controlli di rischio esistenti; solleva `SMTError`:
  - `live_state == configured` → `LIVE_NOT_ARMED` «portafoglio reale sospeso: denaro reale non attivo»;
  - kill switch acceso e non `live_flatten` → `KILL_SWITCH_ACTIVE` (anche per gli ordini che riducono; più severo di R25);
  - vendita oltre la posizione → `SHORT_NOT_ALLOWED`;
  - acquisto: `live_cash_needed > min(cash disponibile, cash del broker convertito se lo snapshot ha ≤ 60 s)` → `LIVE_CASH_LIMIT` con `details {needed, portfolio_cash, broker_cash}`; snapshot vecchio → solo il cash del portafoglio (il ricontrollo con dati freschi lo fa BrokerSync);
  - ordini con `broker_sent_at >= now − 3600 s` ≥ `profile.max_live_orders_per_hour` → `RISK_LIMIT_EXCEEDED` «fusibile: troppi ordini reali nell'ultima ora» + `trip_reduce_only(cause="fuse")` (l'ordine rifiutato viene comunque registrato, come gli altri rifiuti persistenti).
- `BrokerSync` (live, `adapter.live`):
  - `_must_stop`: link non `armed`/`dry_run` → «denaro reale non attivo»;
  - prima di `place_order`: rilegge lo snapshot (con `get_account` se > 60 s; per T212 non più di una volta ogni 5 s) e ripete tetto di liquidità e fusibile; fallimento → `rejected` con il codice;
  - `LiveDryRun` → `rejected`, `reject_code=LIVE_DRY_RUN`, messaggio «prova a secco: ordine non inviato», `broker_raw={"dry_run": payload}` (redatto), `record_live_order_activity(phase="dry_run")`; nessun `broker_sent_at`;
  - `LiveGateClosed` → invio **mai avvenuto** (definitivo): `rejected` con `e.code`, mai `unknown` (Review Focus 2);
  - invio riuscito → `record_live_order_activity("sent")`, `record_live_audit(action="order_sent", order_id, details={payload, response})` senza intestazioni; eseguito/parziale/rifiuto/annullamento → le fasi corrispondenti;
  - `_give_up` su un ordine live → `trip_reduce_only(cause="order_unknown")` oltre al comportamento R7 (Review Focus 1);
  - eseguito live: `execute_fill(..., source="broker", fee_override=bo.fees)` quando `bo.fees` non è None (`engine/execution.py`: `fee_override: Decimal|None = None` sostituisce la commissione calcolata dalle regole); R2 e `FILL_PRICE_WAIT` invariati.

- [ ] **Step 1: test falliti** (`test_live_checks.py` puro dove possibile): `test_cash_needed_formula`, `test_buy_over_min_cash_refused`, `test_stale_snapshot_uses_portfolio_cash_only`, `test_sell_beyond_position_is_short_not_allowed`, `test_kill_switch_blocks_reducing_live_order`, `test_flatten_passes_kill_switch`, `test_configured_is_live_not_armed`, `test_fuse_31st_order_trips_reduce_only` (default 30; profilo a 5 → sesto rifiutato).
- [ ] **Step 2: test falliti** (`test_live_sync.py`, respx `assert_all_mocked`):
  - `test_dry_run_end_to_end` — ordine di un agente via `place_order` → rischio → BrokerSync → `rejected` `LIVE_DRY_RUN`, payload in `broker_raw`, nessun `POST` sul mock, attività `live.order.dry_run`;
  - `test_armed_send_records_sent_activity_and_audit`;
  - `test_gate_closed_after_must_stop_is_rejected_not_unknown` (Review Focus 2);
  - `test_t212_live_unknown_outcome_no_resend_reduce_only` (Review Focus 1);
  - `test_broker_fee_recorded_as_actual_fee`;
  - `test_cash_recheck_before_send_with_fresh_snapshot`;
  - `test_paper_paths_unchanged` (un ordine `alpaca_paper` segue il percorso del piano 6).
- [ ] **Step 3:** `uv run pytest tests/unit/test_live_checks.py tests/integration/test_live_sync.py -q` → FAIL.
- [ ] **Step 4:** implementazione → PASS; `uv run pytest tests/integration/test_brokers_sync.py tests/integration/test_risk_checks.py tests/integration/test_reduce_only.py -q` verdi.
- [ ] **Step 5:** commit `feat(live): live pre-trade checks (cash cap, fuse, no short, strict kill switch) and BrokerSync live send path`

### Task 7: Riconciliazione live, impronta e salute del collegamento

**Files:**
- Create: `brokers/live/reconcile.py`, `brokers/live/monitor.py`
- Modify: `worker/main.py` (`supervise("live monitor", ...)`), `jobs/scheduler.py` (`reconcile_all` a 300 s esclude i link con `live_state` non nullo). `brokers/sync.py` resta del Task 6.
- Test: `tests/integration/test_live_monitor.py`

**Interfaces — Consumes:** Task 2 (`get_adapter`, `get_account`, `get_positions`), 3 (`trip_reduce_only`, `live_alert`), 5 (`test_live_link` per l'impronta), `valuation` FX.

**Interfaces — Produces:**
- `LIVE_RECONCILE_INTERVAL_S = 60`, `LIVE_MONITOR_TICK_S = 5`, `HEALTH_FAILURES_ALERT = 3`.
- `def cash_tolerance(ours: Decimal, eur_in_ccy: Decimal) -> Decimal` = `max(Decimal("0.50") × eur_in_ccy, ours × Decimal("0.001"))`.
- `async def reconcile_live(sessionmaker, link_id, http_factory) -> Reconciliation|None` — per i link con `live_state` non nullo e un portafoglio collegato:
  - I/O fuori transazione; `get_positions` e `get_account`;
  - simboli con ordini aperti già al broker (`broker_order_id` non nullo, stato in `OPEN_STATUSES`) esclusi dal confronto di quel passaggio (registrati in `diffs` come `{"symbol", "skipped": "ordine in corso"}`); con almeno uno di questi ordini il confronto della liquidità viene saltato;
  - differenza di posizione, oppure `broker_cash < ours_cash − tolleranza` (stessa valuta, FX per Alpaca) → riga `Reconciliation(ok=False)` con `diffs` (anche `{"cash": {"ours","broker"}}`), `trip_reduce_only(cause="reconcile_diff" | "cash_shortfall")`;
  - `account_id` diverso dall'impronta → `configured` + `trip_reduce_only(cause="fingerprint_changed")` + `live_alert("account_changed")`;
  - aggiorna `last_account_snapshot`; non corregge nulla.
- `class LiveMonitor(sessionmaker, *, http_factory, clock=..., sleep=asyncio.sleep)`, `run()` e `tick() -> int`: per ogni link live riconcilia se sono passati 60 s dall'ultima riconciliazione **o** se esiste un `Fill` del portafoglio con `ts` successivo all'ultima riconciliazione; ogni errore di rete incrementa `health_failures`, un successo lo azzera; al terzo errore consecutivo `live_alert("unreachable", "broker non raggiungibile")` una sola volta finché non torna raggiungibile; lo stato del link non cambia.
- `http_factory(link_id) -> LiveClient` = `make_live_client` (iniettabile nei test).

- [ ] **Step 1: test falliti:**
  - `test_position_diff_trips_reduce_only_and_alert`;
  - `test_cash_shortfall_beyond_tolerance` (0,49 € sotto → ok; 0,51 € sotto → reduce-only) e `test_broker_cash_surplus_is_ok`;
  - `test_inflight_symbol_skipped_no_false_alarm`;
  - `test_reconcile_after_fill_without_waiting_60s`;
  - `test_fingerprint_change_detected_by_monitor` (Review Focus 3);
  - `test_three_health_failures_alert_once_then_reset`;
  - `test_paper_links_not_in_live_monitor_and_live_not_in_300s_job`;
  - `test_monitor_uses_only_get` (respx: solo route `GET`).
- [ ] **Step 2:** `uv run pytest tests/integration/test_live_monitor.py -q` → FAIL.
- [ ] **Step 3:** implementazione e collegamento nel worker; `tests/unit/test_worker.py` aggiornato se elenca i job.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** commit `feat(live): 60s live reconciliation of positions and cash, fingerprint and health monitor`

### Task 8: API admin, DTO, MCP e CLI

**Files:**
- Create: `admin/routes_live.py`
- Modify: `admin/routes_brokers.py` (kind live rifiutati; DTO con `live_state`), `admin/routes_portfolios.py` e `admin/routes_dashboard.py` (DTO `mode`, `live_state`), `admin/routes_risk.py` (`max_live_orders_per_hour`), `api/main.py`, `mcp/dto.py`, `cli.py` (`smtrading live disarm <link>`, `smtrading live status`)
- Test: `tests/integration/test_admin_live.py`, `tests/integration/test_mcp_portfolio.py` (estensione)

**Interfaces — Consumes:** Task 5 (tutte le funzioni di servizio), 6 (codici), 7.

**Interfaces — Produces** (tutti `require_admin`, `_transaction`, `Actor` da `request_entry`/`User-Agent`, risposte mai con segreti):
- `GET /api/live-links` → `[{id, name, kind, live_state, armed_at, armed_by, disarmed_at, disarmed_reason, key_masked: "••••ABCD", account_masked, snapshot: {currency, cash_available, positions_count, open_orders_count, crypto_enabled, order_types}|None, last_test_at, health_failures, portfolio: {id, name}|None}]`.
- `POST /api/live-links` `{name, kind, key_id|api_key, secret_key|api_secret, crypto_enabled?, order_types?}` → link; `PUT /api/live-links/{id}/keys` (sostituzione).
- `POST /api/live-links/{id}/test` → risultato di `test_live_link`, errori redatti con `link_secrets`.
- `POST /api/live-links/{id}/dry-run` `{on: bool}`.
- `POST /api/live-links/{id}/arm` `{totp, confirm_name, accept_risks, dedicated_account, limits_set}`.
- `POST /api/live-links/{id}/disarm` `{reason?: str}` — nessun TOTP; `reason` default «disattivato dall'admin».
- `PUT /api/portfolios/{id}/live-link` `{link_id: UUID|null, totp}`; `POST /api/portfolios/{id}/live-resume` `{totp}`; `POST /api/portfolios/{id}/live-flatten` `{totp}`.
- `GET /api/live-audit?link_id=&portfolio_id=&action=&before_id=&limit=50` (1-200) e `GET /api/live-audit.csv` (stessi filtri, `text/csv; charset=utf-8`, intestazione `id;ts;collegamento;portafoglio;ordine;attore;azione;ip;dettagli`).
- `GET /api/live/status` → `{armed_count, dry_run_count, portfolios: [{id, name, live_state}], telegram_ok: bool}` (per il banner e l'avviso fisso).
- `POST/PATCH /api/broker-links` con kind in `LIVE_KINDS` → 422 «usa la procedura “denaro reale”».
- Profili di rischio: `max_live_orders_per_hour` in lettura e scrittura (1-1000, 422 fuori intervallo).
- DTO portafoglio (admin e MCP `get_portfolio`, `list_portfolios`): `mode` = `reale` per i kind live, nuovo campo `live_state`. Nessun metodo MCP nuovo, nessun metodo MCP attiva/disattiva/collega; i rifiuti degli ordini MCP espongono `LIVE_NOT_ARMED`, `LIVE_DRY_RUN`, `LIVE_CASH_LIMIT` come gli altri `ErrorCode`.
- CLI: `smtrading live disarm <nome> [--reason]` (emergenza da shell, nessun TOTP), `smtrading live status` (stato dei collegamenti live; «nessun collegamento live» se non ce ne sono). Nessun comando CLI salva chiavi live o arma.

- [ ] **Step 1: test falliti** (respx sul fake; pyotp):
  - `test_wizard_flow_api` — crea → test → dry-run → arm (TOTP) → link portafoglio (TOTP) → disarm;
  - `test_live_kinds_refused_on_paper_routes`;
  - `test_arm_without_admin_totp_403_message`;
  - `test_disarm_needs_no_totp`;
  - `test_live_audit_filters_and_csv`;
  - `test_live_canary_keys_never_leak` — chiavi canarino: nessuna traccia in risposte API (tutte le rotte live), `live_audit`, `audit`, `telegram_outbox`, `orders.broker_raw`, log catturati con `caplog` (anche con un errore 401 del fake che riporta la chiave nel corpo);
  - `test_portfolio_dto_mode_reale_and_live_state` (admin e MCP);
  - `test_mcp_order_on_suspended_real_portfolio_live_not_armed`;
  - `test_risk_profile_fuse_editable_range`;
  - `test_cli_live_disarm`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_admin_live.py tests/integration/test_mcp_portfolio.py -q` → FAIL.
- [ ] **Step 3:** implementazione.
- [ ] **Step 4:** PASS; `uv run pytest -q` verde.
- [ ] **Step 5:** commit `feat(admin): real-money link wizard API, live audit with CSV, live DTOs, MCP error codes, CLI disarm`

### Task 9: Interfaccia web «denaro reale»

**Files:**
- Create: `web/src/components/LiveLinkWizard.tsx`, `LiveArmDialog.tsx`, `LiveOrderConfirm.tsx`, `web/src/pages/LiveAudit.tsx`, `web/src/api/hooks/live.ts`, test Vitest accanto a ciascuno
- Modify: `web/src/pages/Brokers.tsx`, `web/src/components/ModeBanner.tsx`, `web/src/components/PortfolioModeBadge.tsx`, `web/src/pages/PortfolioDetail.tsx` (badge, «Ho verificato, riprendi», «Chiudi tutte le posizioni reali», nota trailing), modulo d'ordine admin, `web/src/pages/Risk.tsx` (campo fusibile), `web/src/routes.tsx` + `Sidebar.tsx` (voce «Registro denaro reale», icona `ShieldAlert`, sotto «Broker»), `web/src/api/types.ts`

**Interfaces — Consumes:** contratto HTTP del Task 8 (mock di fetch come in `Brokers.test.tsx`).

**Interfaces — Produces** (testi esatti):
- Badge: `armed` → «🔴 REALE», `dry_run` → «🔴 REALE · PROVA», `configured` → «🔴 REALE · SOSPESO».
- `ModeBanner`: con `armed_count > 0` barra rossa fissa «Denaro reale attivo su N portafogli» (singolare «su 1 portafoglio»); con `telegram_ok=false` e almeno un collegamento armato, avviso fisso «Telegram non funziona: gli ordini reali continuano, le notifiche restano in coda».
- Pagina Broker: tipi «Alpaca — denaro reale» e «Trading 212 — denaro reale» con bordo rosso; procedura in 4 passi: 1 «Chiavi» (campi password, guida permessi minimi T212: dati del conto, metadati, portafoglio, ordini lettura ed esecuzione, storico; niente «Pies - Write»; restrizione IP consigliata); 2 «Test in sola lettura» (conto mascherato, tipo, valuta, liquidità, posizioni e ordini aperti con la richiesta di zero, cripto sì/no, tipi d'ordine); 3 «Prova a secco» (facoltativa, consigliata); 4 «Attiva denaro reale» (apre `LiveArmDialog`). Pulsante rosso fisso «Disattiva denaro reale», un clic, nessuna conferma TOTP.
- `LiveArmDialog`: il testo dell'avviso dello spec (§9, «Stai per attivare il trading con denaro reale.» … «Scrivi il nome del collegamento e inserisci il codice TOTP per confermare.») con i segnaposto riempiti; tre spunte; campo nome; campo TOTP; pulsante «Attiva» disabilitato finché tutto non è compilato; errore `field=totp` con TOTP non attivo → «Attiva prima l'autenticazione a due fattori» con link a Impostazioni.
- `LiveOrderConfirm`: per gli ordini manuali dell'admin su un portafoglio reale, finestra «Ordine con denaro reale» con riepilogo; un clic, senza TOTP. Per i trailing stop la nota «stop gestito da SMTrading, non dal broker».
- `LiveAudit`: `DataTable` filtrabile per collegamento, portafoglio, azione (etichette italiane), «Carica altri», «Esporta CSV».

- [ ] **Step 1: test falliti:** `badge variants per live_state`, `banner red with count and telegram warning`, `wizard shows read-only test result and requires empty account`, `arm button disabled until checks, name and totp`, `arm dialog renders warning text with placeholders`, `disarm is one click without totp`, `real order confirm dialog for admin orders`, `resume and flatten ask for totp`, `live audit filters and csv link`, `risk fuse field validates 1-1000`.
- [ ] **Step 2:** `npm --prefix web test -- --run` sui nuovi file → FAIL.
- [ ] **Step 3:** implementazione con i componenti esistenti.
- [ ] **Step 4:** PASS; `npm --prefix web run lint` e `npm --prefix web run build` puliti.
- [ ] **Step 5:** commit `feat(web): real-money wizard, arm dialog, red banner and badges, live audit page`

### Task 10: E2E con broker finto, documentazione e verifica finale

**Files:**
- Create: `backend/src/smtrading/devtools/__init__.py`, `devtools/live_e2e_seed.py`, `web/e2e/live.spec.ts`
- Modify: `scripts/e2e.sh` (variabile `SMT_LIVE_FAKE_BROKER=1` solo per lo stack e2e), `web/e2e/helpers.ts`, `docs/paperclip-mcp.md`, `README.md` (sezione «Denaro reale», senza host live)

**Interfaces — Consumes:** tutto il piano.

**Interfaces — Produces:**
- `python -m smtrading.devtools.live_e2e_seed` — esce con errore se `live_fake_broker` è falso; altrimenti attiva il TOTP dell'admin e2e con un segreto di test noto alla spec Playwright, inserisce una riga `telegram_outbox(category="test", status="sent", sent_at=now())` e una configurazione Telegram pronta (token finto, invio verso un sender disattivato), crea un portafoglio vuoto con profilo a norma.
- `live.spec.ts`: procedura completa contro il `FakeLiveBroker` (chiavi canarino → test → prova a secco → attiva con TOTP generato da `otpauth` → collega portafoglio → badge «🔴 REALE» e barra rossa → «Disattiva denaro reale» a un clic → badge «🔴 REALE · SOSPESO»).
- `docs/paperclip-mcp.md`: paragrafo «🔴 un portafoglio `reale` usa denaro vero»; `live_state`; codici `LIVE_NOT_ARMED`, `LIVE_DRY_RUN`, `LIVE_CASH_LIMIT`; nessun metodo MCP attiva o collega conti reali; il kill switch blocca anche le chiusure.
- `README.md`: la checklist «Cosa deve fare l'utente al momento dell'attivazione» dello spec, i comandi di emergenza (`Disattiva denaro reale`, kill switch, `smtrading live disarm`) e la nota fiscale come rimando.

- [ ] **Step 1:** `live.spec.ts` scritto → `bash scripts/e2e.sh` → FAIL finché seed e helper non esistono.
- [ ] **Step 2:** seed, helper, documentazione → `bash scripts/e2e.sh` verde.
- [ ] **Step 3: verifica:**
  1. `cd backend && uv run pytest -q` → verde, nessun test saltato tra quelli live;
  2. `npm --prefix web test -- --run` → verde;
  3. `uv run pytest tests/unit/test_live_hosts_guard.py -q` → PASS;
  4. `docker compose up -d --build` **senza** `SMT_LIVE_FAKE_BROKER`; `alembic current` = head;
  5. `docker compose exec -T api smtrading live status` → «nessun collegamento live» (0 collegamenti `alpaca_live`/`t212_live`);
  6. `docker compose exec -T api python -m smtrading.devtools.live_e2e_seed` → esce con errore (flag spento);
  7. `git grep -l 'CANARY' -- ':!backend/tests' ':!docs' ':!web/e2e'` → nessun risultato; `git status --porcelain` non mostra `.env` né file di segreti.
- [ ] **Step 4:** commit `docs: real-money guide, paperclip notes, e2e wizard against the fake live broker`
- [ ] **Step 5 (controller):** nessuna attivazione. Annotare in memoria di progetto: «piano 8 chiuso, codice pronto, nessun collegamento live creato; attivazione = checklist README con l'utente».
