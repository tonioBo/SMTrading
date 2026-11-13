# SMTrading — Piano 7: Notifiche Telegram e agente Notifier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** un gruppo Telegram riceve da SMTrading gli eventi critici e gli ordini eseguiti o rifiutati (anche con Paperclip fermo), più i riepiloghi delle 07:00, delle 18:00 e del venerdì alle 18:30; l'agente «Notifier» invia tramite MCP decisioni e commenti; tutte le decisioni degli agenti restano nello storico.

**Architecture:**
- **Feed attività (`activity`):** una riga per ogni fatto rilevante, scritta **nella stessa transazione** del fatto: `events.queue_event` (ordini, kill switch, reset, depositi) e `events.notify` (liquidazione, margine, perdita giornaliera, broker, alert) chiamano `record_activity`. Niente si perde se Redis o il worker sono fermi; un rollback cancella anche l'attività.
- **Invio diretto:** le righe con `telegram='pending'` vengono pianificate dal `TelegramPlanner` del worker (outbox subito, digest delle 07:00 nelle ore notturne, `skipped` se Telegram è disattivato). Il `OutboxSender` svuota `telegram_outbox` con backoff esponenziale, rispetta il `retry_after` di Telegram e il limite del gruppo.
- **Agenti:** `log_decision`, `get_decisions`, `get_activity_feed` per tutti; `send_telegram` solo con `agents.can_notify` (il permesso `notify`), che passa sempre dalla outbox.
- **Riepiloghi:** il `SummaryScheduler` del worker (ore Europe/Rome) costruisce digest notturno, riepilogo giornaliero e settimanale con la classifica dei portafogli.
- Configurazione in `app_settings` (chiave `telegram`), token cifrato con `common.crypto`. Pagina admin «Notifiche». Il piano parte dal commit finale del piano 6, ramo `feat/plan-07-telegram`.

**Tech Stack:** come i piani 1-6 (FastAPI, SQLAlchemy async, Alembic, httpx, respx, mcp 2.x, React + TanStack Query + Vitest). Nessuna dipendenza nuova: Telegram Bot API via httpx.

**Spec:** `docs/superpowers/specs/2026-11-13-telegram-notifications-design.md` (inclusa la sezione «Decisioni dell'utente»).

## Global Constraints

- Valgono i vincoli dei piani 1-6: importi `Decimal` (mai float), JSON con numeri come stringhe (`json_safe`), testi per l'utente in italiano, errori `SMTError` con `ErrorCode`.
- **Segreti:** il token del bot non compare mai in log, errori, risposte API, audit, `last_error` o eccezioni concatenate. Si riusano `RedactingFilter` (`common/logsetup.py`), `MASK`/`mask_value`/`redact` (`admin/common.py`).
- **Telegram:** base URL fissa `https://api.telegram.org`; metodi `getMe`, `getChat`, `getUpdates`, `sendMessage`. `sendMessage` **senza `parse_mode`**; l'unica formattazione è l'entità `pre` (tabella della classifica) passata in `entities`, con offset e lunghezze in unità UTF-16. Prima della stesura l'implementer verifica i formati su `https://core.telegram.org/bots/api` e annota nel report gli URL consultati.
- **Limiti:** testo ≤ 4096 caratteri per messaggio; `send_telegram` ≤ 20 messaggi per agente in 60 s scorrevoli; il sender non invia più di un messaggio ogni `SEND_MIN_INTERVAL_S = 3` s al gruppo (limite Telegram ~20/min per gruppo); al massimo `MAX_ATTEMPTS = 10` tentativi con backoff `min(5 × 2^(n−1), 900)` s; un 429 non consuma tentativi e aspetta `parameters.retry_after`.
- **Ore notturne:** 23:00 ≤ ora locale < 07:00, fuso `get_settings().tz` (Europe/Rome). Di notte: eventi `critical` inviati subito con `disable_notification=true`; gli altri eventi diretti vanno nel digest delle 07:00; i messaggi degli agenti partono subito, silenziosi. Il silenzio si valuta all'**invio**, non all'accodamento.
- **Prefissi:** 🔴 `critical`, 🟠 `warning`, 🟢 `executed`, nessun prefisso per `info`.
- **Riepiloghi:** giornaliero ogni giorno alle 18:00, settimanale il venerdì alle 18:30, digest alle 07:00 (solo se ci sono eventi), ora locale. Idempotenti con `job_runs`; un riepilogo non inviato entro le 23:00 del suo giorno viene saltato (log WARNING), niente recuperi dei giorni passati.
- **Classifica:** portafogli non archiviati (`live_portfolio_ids()`), colonne rendimento %, max drawdown %, n. operazioni (fill) nel periodo; ordinata per rendimento decrescente, `None` in fondo; numeri a 2 decimali con virgola italiana.
- **Migrazione:** il numero successivo libero dopo il piano 6 (a HEAD `435a94a` l'ultima è `0014_risk_profile_description.py`, quindi `0015_telegram.py` se il piano 6 non ne aggiunge altre). Qui sotto: `00NN_telegram.py`.
- **Audit admin:** ogni modifica passa da `write_audit` con `method="admin:telegram.*"` o `"admin:agent.notify"`, `params` senza token.

## Review Focus

1. **Gruppo trasformato in supergruppo** (Telegram risponde 400 con `parameters.migrate_to_chat_id`) → la configurazione prende il nuovo `chat_id`, il messaggio viene reinviato subito senza consumare un tentativo, riga di audit `admin:telegram.chat_migrated` con `by="system"`. Test in Task 4.
2. **Telegram irraggiungibile per ore o in 429** → la outbox conserva tutto in ordine di id; il 429 aspetta `retry_after` senza contare il tentativo; al ritorno di Telegram i messaggi arrivano nell'ordine originale; dopo 10 errori diversi dal 429 → `failed`, che si può riprovare. Test in Task 4.
3. **Testo dell'agente con caratteri di markup** (`<b>`, `*_[]()`, `@`) → inviato identico, payload senza `parse_mode`; i caratteri di controllo tranne `\n` e `\t` vengono rimossi; testo vuoto dopo lo strip → `INVALID_ORDER`. Test in Task 3 (payload) e Task 6 (tool).
4. **Confini delle ore notturne e cambio d'ora** → 22:59:59 non è notte, 23:00:00 sì, 06:59:59 sì, 07:00:00 no, anche il 25-26 ottobre 2026 (fine dell'ora legale) e il 29 marzo 2026; un evento non critico alle 23:00:00 va nel digest. Test in Task 3 (puro) e Task 4 (planner).
5. **Riepilogo più lungo di 4096 caratteri** (molti portafogli o eventi) → diviso a confine di riga in più messaggi ≤ 4096, ciascuno con la propria entità `pre` dagli offset UTF-16 corretti anche con le emoji del prefisso. Test in Task 3 (`split_message`) e Task 5.

---

## File Structure

```
backend/src/smtrading/
  activity/__init__.py
  activity/models.py        Activity, Decision, DecisionKind, ActivityLevel, TelegramState
  activity/mapping.py       puro: evento/notifica -> ActivityDraft (tipo, livello, diretto, sommario)
  activity/service.py       record_activity, log_decision, list_decisions, list_feed
  telegram/__init__.py
  telegram/models.py        TelegramOutbox, OutboxStatus
  telegram/client.py        TelegramClient, TelegramError (messaggi italiani, token redatto)
  telegram/config.py        TelegramConfig, load_config, save_config, config_dto
  telegram/render.py        puro: prefissi, render_activity, split_message, ranking_table
  telegram/quiet.py         puro: is_quiet(now, tz)
  telegram/outbox.py        enqueue, agent_sent_last_minute, resume_paused, retry
  telegram/planner.py       TelegramPlanner (activity pending -> outbox/digest/skipped)
  telegram/sender.py        OutboxSender
  telegram/summaries.py     SummaryScheduler, build_digest, build_summary, ranking
  mcp/tools_activity.py     log_decision, get_decisions, get_activity_feed
  mcp/tools_telegram.py     send_telegram
  admin/routes_telegram.py  /api/telegram/*, /api/decisions
backend/migrations/versions/00NN_telegram.py
web/src/pages/Notifications.tsx, Notifications.test.tsx
web/src/api/hooks/telegram.ts
backend/tests/unit/test_activity_mapping.py, test_telegram_render.py, test_telegram_quiet.py
backend/tests/integration/test_activity.py, test_telegram_client.py, test_telegram_worker.py,
  test_telegram_summaries.py, test_mcp_activity_telegram.py, test_admin_telegram.py
```

## Parallelismo e proprietà dei file

| Ondata | Task | Note |
|---|---|---|
| A | 1 | schema, prerequisito di tutto |
| B | 2 ∥ 3 | 2 possiede `activity/*`, `events.py`, `brokers/sync.py`; 3 possiede `telegram/{client,config,render,quiet,outbox}.py`, `common/logsetup.py` |
| C | 4 ∥ 6 ∥ 7 ∥ 8 | 4: `telegram/{planner,sender}.py`, `worker/main.py`; 6: `mcp/tools_*` nuovi, `mcp/server.py`; 7: `admin/routes_telegram.py`, `admin/routes_agents.py`, `api/main.py`, `cli.py`; 8: solo `web/` (contratto API fissato nel Task 7) |
| D | 5 | dopo il 4: aggiunge una riga a `worker/main.py` |
| E | 9 | documentazione, e2e, verifica finale |

---

### Task 1: Schema — attività, decisioni, outbox, permesso `notify`

**Files:**
- Create: `activity/__init__.py`, `activity/models.py`, `telegram/__init__.py`, `telegram/models.py`, `migrations/versions/00NN_telegram.py`
- Modify: `agents/models.py` (`Agent.can_notify`), `agents/service.py` (`set_agent_can_notify`), `models.py` (import dei nuovi modelli nel registro)
- Test: `tests/integration/test_migrations.py` (drift già esistente), `tests/integration/test_agents.py`

**Interfaces — Produces:**
- `class ActivityLevel(StrEnum)`: `info, warning, critical, executed`.
- `class TelegramState(StrEnum)`: `pending, queued, digest, skipped`.
- `class DecisionKind(StrEnum)`: `proposal, approval, veto, allocation, strategy, risk_rule, other`.
- `Activity` (`activity`): `id BIGINT identity PK; ts timestamptz server_default clock_timestamp(); type String(64); level String(16); agent_id UUID FK agents SET NULL|None; portfolio_id UUID FK portfolios|None; summary String(500); data JSONB default {}; telegram String(16)|None; outbox_id BIGINT FK telegram_outbox SET NULL|None`. Indici: `(portfolio_id, id)`, parziale `(id) WHERE telegram IN ('pending','digest')`.
- `Decision` (`decisions`): `id BIGINT identity; agent_id UUID FK agents; portfolio_id UUID FK|None; kind String(16) con CHECK sui 7 valori; summary String(500); details JSONB default {}; symbols JSONB list default []; created_at server_default now()`. Indici `(portfolio_id, id)`, `(agent_id, id)`. Append-only (nessuna funzione di update/delete).
- `class OutboxStatus(StrEnum)`: `pending, sent, failed, paused`.
- `TelegramOutbox` (`telegram_outbox`): `id BIGINT identity; source String(64)` (`system` | `agent:<uuid>`); `category String(16)` (`event|digest|daily|weekly|agent|test`); `level String(16); text Text; entities JSONB|None; status String(16); attempts int default 0; next_attempt_at timestamptz server_default now(); last_error String(500)|None; telegram_message_id BIGINT|None; created_at server_default now(); sent_at|None`. Indici `(status, next_attempt_at, id)`, `(source, created_at)`.
- `Agent.can_notify: bool` default false, `server_default="false"`.
- `async def set_agent_can_notify(session, agent_id: UUID, can_notify: bool) -> Agent` (NOT_FOUND se manca).
- Configurazione: nessuna tabella; `app_settings` chiave `"telegram"` (Task 3).

- [ ] **Step 1: test falliti** — `test_migration_matches_models` (drift) e in `test_agents.py`:
  ```python
  async def test_can_notify_defaults_false_and_toggles(db_session):
      agent, _ = await create_agent(db_session, "Notifier")
      assert agent.can_notify is False
      assert (await set_agent_can_notify(db_session, agent.id, True)).can_notify is True
  async def test_decision_kind_check_constraint(db_session): ...  # kind="foo" -> IntegrityError
  ```
- [ ] **Step 2:** `cd backend && uv run pytest tests/integration/test_migrations.py tests/integration/test_agents.py -q` → FAIL.
- [ ] **Step 3:** modelli, migrazione (upgrade e downgrade), funzione di servizio.
- [ ] **Step 4:** stesso comando → PASS; `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` senza errori.
- [ ] **Step 5:** commit `feat: schema for activity feed, decisions, telegram outbox, notify permission`

### Task 2: Registrazione delle attività, decisioni e query del feed

**Files:**
- Create: `activity/mapping.py`, `activity/service.py`
- Modify: `events.py` (`queue_event`, `notify` chiamano `record_activity`), `brokers/sync.py` (`reconcile`: attività per le differenze)
- Test: `tests/unit/test_activity_mapping.py`, `tests/integration/test_activity.py`

**Interfaces — Consumes:** modelli del Task 1.

**Interfaces — Produces:**
- `@dataclass(frozen=True) ActivityDraft: type: str; level: ActivityLevel; direct: bool; summary: str; data: dict`
- `def activity_for_event(type: str, payload: dict) -> ActivityDraft | None` — tabella fissa (tutto il resto → None, in particolare `notification`, `liquidation`, `margin.warning`, `order.cancel_requested`, `order.broker_accepted`, `portfolio.created/archived/unarchived`):

  | evento | livello | diretto |
  |---|---|---|
  | `order.accepted` | info | no |
  | `order.filled` | executed | **sì** |
  | `order.partially_filled` | info | no |
  | `order.rejected` | warning | **sì** |
  | `order.canceled`, `order.expired` | info | no |
  | `kill_switch` on / off | critical / warning | **sì** |
  | `portfolio.reset`, `portfolio.cash` | info | no |

- `def activity_for_notification(kind: str, payload: dict) -> ActivityDraft` — `liquidation`, `daily_loss`, `broker.order_unknown` → critical diretto; `margin_warning`, `order.rejected`, `broker.liquidation_waiting`, `broker.fill_price_missing`, `order.replace_failed` → warning diretto; ogni altro kind (`price_alert`, `instrument.added`, `admin.watch_changed`, …) → info non diretto. Tipo dell'attività = il kind.
- Sommari italiani brevi (≤ 500, troncati), per esempio `"Ordine eseguito: buy 0.1 BTC-USD a 50000 USD"`, `"Kill switch attivato (3 ordini annullati)"`, `"Deposito di 1000 EUR"`. Il formato esatto è libero; i valori numerici vengono dal payload come stringhe.
- `def record_activity(session, draft: ActivityDraft, *, portfolio_id: UUID|None, agent_id: UUID|str|None) -> None` — sincrona, `session.add(Activity(..., telegram="pending" if draft.direct else None))`. Deduplica nella sessione: chiave `(draft.type, data.get("order_id"))` quando c'è `order_id`, salvata in `session.info["smt_activity_keys"]` (un rifiuto annunciato sia da `notify` sia da `queue_event` nella stessa transazione genera una sola riga). Pulizia della chiave in `discard_post_commit` e dopo `dispatch_post_commit`.
- In `events.py`: `queue_event` chiama `activity_for_event` e, se non None, `record_activity(portfolio_id=payload.get("portfolio_id"), agent_id=payload.get("agent_id"))`; `notify` chiama `activity_for_notification`. `agent_id` str viene convertito in UUID; uno non valido → None.
- In `reconcile` (brokers/sync.py), per ogni `Reconciliation` con `ok=False`: `record_activity(ActivityDraft("broker.reconciliation_diff", warning, True, "Riconciliazione broker con N differenze", {"broker_link_id", "diffs"}), portfolio_id=pid, agent_id=None)`.
- `async def log_decision(session, agent_id: UUID, *, kind: str, summary: str, portfolio_id: UUID|None, symbols: list[str]|None, details: dict|None) -> Decision`:
  - `kind` fuori dai 7 valori, `summary` vuoto dopo lo strip o > 500, più di 50 simboli o simbolo > 32 caratteri, `details` non dict o JSON > 8192 byte → `INVALID_ORDER` con `details.field`;
  - con `portfolio_id`: `require_permission(..., "read")`;
  - simboli in maiuscolo, senza duplicati, non verificati sul catalogo;
  - registra anche `Activity(type=f"decision.{kind}", level=info, telegram=None, data={"decision_id", "symbols", "details"})`.
- Visibilità comune (feed e decisioni) per un agente: `portfolio_id IN agent_portfolio_ids(...)` **oppure** `portfolio_id IS NULL AND (agent_id IS NULL OR agent_id = me OR type LIKE 'decision.%')`.
- `async def list_decisions(session, *, viewer_agent_id: UUID|None, portfolio_id=None, agent_id=None, kind=None, since: datetime|None=None, before_id: int|None=None, limit: int=50) -> list[Decision]` — `viewer_agent_id=None` = admin, tutto visibile; ordine id decrescente.
- `FEED_SETTLE_S = 3`. `async def list_feed(session, viewer_agent_id: UUID, *, after_id: int|None, since: datetime|None, limit: int) -> tuple[list[dict], int|None]` — stesso contratto di `get_triggered_alerts`: senza cursore né `since` le ultime `limit` dalla più recente e `next_after_id` = id più recente; con cursore o `since` in ordine crescente di id, solo righe con `ts <= now() - FEED_SETTLE_S`, `next_after_id` = ultimo id restituito o il cursore stesso. `after_id` sconosciuto → `INVALID_ORDER` `field=after_id`. Elemento: `{id, ts, type, level, agent, agent_id, portfolio, portfolio_id, summary, data, already_sent}` con `agent` e `portfolio` = nomi (join), `already_sent = telegram in ("queued","digest")` (SMTrading lo invia da sé).

- [ ] **Step 1: test falliti** (`test_activity_mapping.py`, puro):
  - `test_filled_is_executed_and_direct`, `test_kill_switch_on_critical_off_warning`, `test_liquidation_event_ignored_notification_critical`, `test_unknown_event_is_none`, `test_price_alert_notification_info_not_direct`, `test_summary_truncated_to_500`.
- [ ] **Step 2: test falliti** (`test_activity.py`, integrazione):
  - `test_place_and_fill_records_accepted_and_filled` → 2 righe, la seconda con `telegram="pending"`;
  - `test_rollback_drops_activity` → `queue_event` poi rollback → 0 righe;
  - `test_reject_via_notify_and_event_deduped` → una sola riga `order.rejected`;
  - `test_kill_switch_records_critical_pending`;
  - `test_reconcile_diff_records_activity` (con il `FakeBrokerAdapter` di `test_brokers_sync.py`);
  - `test_log_decision_requires_read`, `test_log_decision_validation` (kind, 501 caratteri, 51 simboli, details 9 KB), `test_log_decision_writes_activity`;
  - `test_feed_cursor_contract` (prima chiamata dalla più recente; poi crescente; cursore invariato se vuoto; `after_id` sconosciuto);
  - `test_feed_settle_window` (riga con `ts=now()` esclusa con il cursore, inclusa dopo aver spostato `ts` indietro di 4 s);
  - `test_feed_visibility` (portafoglio non assegnato escluso; `price_alert` di un altro agente escluso; decisione globale di un altro agente inclusa; kill switch incluso);
  - `test_feed_already_sent_flag` (`telegram` pending → false, queued → true, digest → true, None → false).
- [ ] **Step 3:** `uv run pytest tests/unit/test_activity_mapping.py tests/integration/test_activity.py -q` → FAIL.
- [ ] **Step 4:** implementazione; poi tutta la suite `uv run pytest -q` per verificare che l'hook in `events.py` non rompa i test esistenti (ordini, broker, rischio).
- [ ] **Step 5:** PASS → commit `feat(activity): in-transaction activity feed, decisions log, feed queries`

### Task 3: Nucleo Telegram — client, configurazione, rendering, ore notturne, outbox

**Files:**
- Create: `telegram/client.py`, `telegram/config.py`, `telegram/render.py`, `telegram/quiet.py`, `telegram/outbox.py`
- Modify: `common/logsetup.py` (pattern del token Telegram)
- Test: `tests/unit/test_telegram_render.py`, `tests/unit/test_telegram_quiet.py`, `tests/unit/test_worker_logging.py` (estensione), `tests/integration/test_telegram_client.py` (respx)

**Interfaces — Consumes:** `TelegramOutbox`, `OutboxStatus`, `AppSetting`.

**Interfaces — Produces:**
- `class TelegramError(Exception)`: `message: str` (italiano, senza token), `status: int|None`, `retry_after: int|None`, `migrate_to_chat_id: int|None`. Costruita sempre `from None`.
- `class TelegramClient(token: str, http: httpx.AsyncClient, *, timeout: float = 10.0)`, tutti async:
  - `get_me() -> dict` (`{id, username, first_name}`);
  - `get_chat(chat_id: int|str) -> dict` (`{id, type, title}`);
  - `get_updates(limit: int = 100) -> list[dict]` (solo lettura: nessun `offset`, così non consuma gli update);
  - `send_message(chat_id, text: str, *, entities: list[dict]|None = None, disable_notification: bool = False) -> int` (message_id). Il payload JSON ha solo `chat_id`, `text`, `entities` (se presenti), `disable_notification`, `link_preview_options: {"is_disabled": true}`; **mai `parse_mode`**.
  - Mappatura errori: 401 → «Token del bot non valido»; 400 con `chat not found` → «Gruppo non trovato: controlla l'ID e che il bot sia nel gruppo»; 403 → «Il bot non è membro del gruppo o è stato rimosso»; 409 su getUpdates → «Il bot ha un webhook attivo: rimuovilo con deleteWebhook»; 429 → «Telegram chiede di attendere N s»; `httpx.HTTPError` → «Telegram non raggiungibile»; altro → «Errore Telegram (codice N): <description redatta>».
- `common/logsetup.py`: `_TELEGRAM_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")` sostituito con `<redacted>` in `redact_log_text`.
- `@dataclass TelegramConfig: bot_token: str|None; chat_id: str|None; enabled: bool; paused: bool; bot_username: str|None; chat_title: str|None`.
  - `async def load_config(session) -> TelegramConfig` (default tutto None/False);
  - `async def save_config(session, cfg: TelegramConfig) -> None` — `app_settings["telegram"] = {"token_enc": base64(encrypt_json({"bot_token": ...}, master_key)), "chat_id", "enabled", "paused", "bot_username", "chat_title"}`;
  - `def config_dto(cfg) -> dict` — `{configured, enabled, paused, bot_token_masked: "••••"|None, bot_username, chat_id, chat_title}`; il token non viene mai restituito, nemmeno le ultime 4 cifre (solo `MASK`).
  - `def is_ready(cfg) -> bool` = token e chat presenti ed `enabled`.
- `quiet.py`: `QUIET_START = time(23)`, `QUIET_END = time(7)`; `def is_quiet(now: datetime, tz: str) -> bool`; `def next_quiet_end(now, tz) -> datetime` (prossime 07:00 locali, in UTC).
- `render.py`:
  - `PREFIX = {critical: "🔴 ", warning: "🟠 ", executed: "🟢 ", info: ""}`;
  - `MAX_LEN = 4096`;
  - `@dataclass Names: agents: dict[UUID,str]; portfolios: dict[UUID,str]`;
  - `def render_activity(a: Activity, names: Names, tz: str) -> str` → `"{prefisso}{titolo} — {portafoglio}\n{sommario}\nAgente: {nome} · {HH:MM}"` (righe senza dati omesse);
  - `def utf16_len(s: str) -> int`;
  - `def split_message(text: str, pre_block: str|None = None) -> list[tuple[str, list[dict]|None]]` — divide a confine di riga in parti ≤ 4096 (una riga più lunga si taglia); il blocco `pre` segue il testo e riceve un'entità `{"type":"pre","offset":utf16_len(prima),"length":utf16_len(blocco)}` in ogni parte che lo contiene;
  - `def clean_text(text: str) -> str` — rimuove i caratteri di controllo tranne `\n` e `\t`, strip.
- `outbox.py`:
  - `async def enqueue(session, *, source: str, category: str, level: str, text: str, entities: list|None = None) -> TelegramOutbox` — stato `paused` se `load_config().paused`, altrimenti `pending`; `len(text) > 4096` → `ValueError` (i chiamanti dividono prima);
  - `async def agent_sent_last_minute(session, agent_id: UUID) -> int` — prende `pg_advisory_xact_lock(hashtext('tg:' || agent_id))` e conta le righe `source=f"agent:{agent_id}"` con `created_at > now() - 60 s`;
  - `async def resume_paused(session) -> int` (paused → pending, `next_attempt_at=now()`);
  - `async def retry_outbox(session, outbox_id: int) -> TelegramOutbox` (solo `failed` → pending, attempts 0, `last_error` None; altrimenti `INVALID_ORDER`).

- [ ] **Step 1: test falliti** (puri):
  - `test_quiet_boundaries` — 22:59:59 False, 23:00:00 True, 06:59:59 True, 07:00:00 False (ora locale Rome), anche `2026-10-25` e `2026-03-29` (Review Focus 4);
  - `test_next_quiet_end_across_dst`;
  - `test_split_message_lines_and_limit` — 3000 + 3000 caratteri → 2 parti ≤ 4096, nessuna riga spezzata;
  - `test_split_pre_entity_utf16_offsets` — testo con «🔴» (2 unità UTF-16) prima del blocco: `offset == utf16_len(prefisso)` (Review Focus 5);
  - `test_render_filled_has_green_prefix_and_names`, `test_clean_text_strips_controls_keeps_newlines`;
  - `test_log_redacts_telegram_token` — `"https://api.telegram.org/bot123456789:AAH...35caratteri/sendMessage"` → nessuna traccia del segreto.
- [ ] **Step 2: test falliti** (respx, `test_telegram_client.py`):
  - `test_send_message_payload_plain_text` — testo `"<b>*_[x](y)@all"` inviato identico, `"parse_mode" not in json` (Review Focus 3);
  - `test_errors_mapped_italian_without_token` — 401, 400 chat not found, 403, 429 (`retry_after=7`), 400 con `migrate_to_chat_id=-100123`, `httpx.ConnectError`: `str(e)` e `repr(e.__cause__)` non contengono il token, `e.__suppress_context__` vero;
  - `test_get_updates_has_no_offset`;
  - `test_config_roundtrip_encrypted` — `app_settings` grezzo non contiene il token; `config_dto` non contiene il token né le sue ultime cifre;
  - `test_enqueue_paused_when_config_paused`, `test_retry_only_failed`, `test_agent_rate_counter_window`.
- [ ] **Step 3:** `uv run pytest tests/unit/test_telegram_render.py tests/unit/test_telegram_quiet.py tests/unit/test_worker_logging.py tests/integration/test_telegram_client.py -q` → FAIL.
- [ ] **Step 4:** implementazione → PASS.
- [ ] **Step 5:** commit `feat(telegram): bot client, encrypted config, rendering, quiet hours, outbox`

### Task 4: Worker — planner e sender della outbox

**Files:**
- Create: `telegram/planner.py`, `telegram/sender.py`
- Modify: `worker/main.py` (due `supervise`)
- Test: `tests/integration/test_telegram_worker.py` (respx, orologio iniettato)

**Interfaces — Consumes:** Task 2 (`Activity.telegram`), Task 3 (`TelegramClient`, `load_config`, `save_config`, `render_activity`, `is_quiet`, `enqueue`).

**Interfaces — Produces:**
- `class TelegramPlanner(sessionmaker, *, clock=..., tz: str = get_settings().tz, interval_s: float = 2.0)`, `run()` e `tick() -> int`:
  - prende fino a 100 righe `Activity.telegram == "pending"` per id (`FOR UPDATE SKIP LOCKED`);
  - `not is_ready(cfg)` → `skipped`;
  - `is_quiet(now)` e livello ≠ critical → `digest`;
  - altrimenti `enqueue(source="system", category="event", level=a.level, text=render_activity(...))` → `telegram="queued"`, `outbox_id`;
  - una transazione per lotto.
- `class OutboxSender(sessionmaker, http, *, clock=..., tz=..., sleep=asyncio.sleep, interval_s: float = 1.0)`, `run()` e `tick() -> int`:
  - rilegge la configurazione a ogni tick; se non è pronta non invia nulla;
  - prende la riga `pending` con `next_attempt_at <= now` e id minimo (`FOR UPDATE SKIP LOCKED`), una alla volta, rispettando `SEND_MIN_INTERVAL_S` dall'ultimo invio;
  - `disable_notification = is_quiet(now)`;
  - successo → `sent`, `sent_at`, `telegram_message_id`;
  - 429 → `next_attempt_at = now + retry_after`, tentativi invariati, e **nessun altro invio** fino a quel momento (pausa globale);
  - `migrate_to_chat_id` → `save_config(chat_id=nuovo)`, `write_audit(method="admin:telegram.chat_migrated", params={"old","new","by":"system"})`, reinvio immediato senza contare il tentativo (Review Focus 1);
  - altri errori → `attempts += 1`, `last_error = e.message[:500]`, `next_attempt_at = now + min(5 × 2^(attempts−1), 900) s`; con `attempts == 10` → `failed`;
  - un messaggio bloccato in backoff non blocca i successivi (ordine per `next_attempt_at, id`), tranne la pausa globale del 429.
- `worker/main.py`: `supervise("telegram planner", lambda: TelegramPlanner(sessionmaker).run())` e `supervise("telegram sender", lambda: OutboxSender(sessionmaker, http).run())`.

- [ ] **Step 1: test falliti:**
  - `test_fill_reaches_group` — ordine eseguito → planner → sender → una POST `sendMessage` con «🟢» e `disable_notification=false`;
  - `test_disabled_config_skips`;
  - `test_quiet_noncritical_goes_to_digest_critical_sent_silent` — 23:00:00 Rome: fill → `digest`; kill switch → inviato con `disable_notification=true` (Review Focus 4);
  - `test_paused_then_resumed` — pausa: righe `paused`, nessuna POST; `resume_paused` → inviate in ordine;
  - `test_429_respects_retry_after_without_attempt` (Review Focus 2);
  - `test_outage_then_recovery_keeps_order` — 3 messaggi, `ConnectError` per due tick, poi OK → arrivano in ordine di id;
  - `test_ten_failures_mark_failed` e backoff 5, 10, 20 … 900 s;
  - `test_supergroup_migration_updates_config_and_resends` (Review Focus 1);
  - `test_last_error_has_no_token`;
  - `test_min_interval_between_sends`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_telegram_worker.py -q` → FAIL.
- [ ] **Step 3:** implementazione e collegamento nel worker; `tests/unit/test_worker.py` aggiornato se elenca i job.
- [ ] **Step 4:** PASS, poi `uv run pytest tests/unit/test_worker.py -q`.
- [ ] **Step 5:** commit `feat(telegram): activity planner and outbox sender with retries, 429 and quiet hours`

### Task 5: Riepiloghi programmati e classifica

**Files:**
- Create: `telegram/summaries.py`
- Modify: `worker/main.py` (`supervise("telegram summaries", ...)`)
- Test: `tests/integration/test_telegram_summaries.py`

**Interfaces — Consumes:** Task 2 (`Activity`, `Decision`), Task 3 (`enqueue`, `split_message`, `is_ready`), `performance.service.get_performance`, `jobs.runs.run_job_once`, `live_portfolio_ids`, `engine.models.Fill` (tabella `fills`).

**Interfaces — Produces:**
- `@dataclass RankRow: portfolio: str; return_pct: Decimal|None; max_drawdown_pct: Decimal|None; trades: int`
- `async def ranking(session, *, period: Literal["1d","1w"], since: datetime, now: datetime) -> list[RankRow]` — `get_performance(period=...)` per ogni portafoglio non archiviato; `trades` = numero di fill con `ts >= since`; ordinata per `return_pct` decrescente con i `None` in fondo, a parità per nome.
- `def ranking_table(rows: list[RankRow]) -> str` (in `render.py` se l'implementer preferisce; resta puro) — colonne a larghezza fissa: `Portafoglio | Rend. % | Max DD % | Op.`, numeri `+1,23` / `-0,50` / `—`, nomi troncati a 18 caratteri.
- `async def build_digest(session, now) -> list[tuple[str, list|None]] | None` — attività `telegram="digest"`; None se non ce ne sono; testo «🌙 Riepilogo della notte» con una riga per evento (`HH:MM` + sommario), raggruppate per livello; le righe incluse diventano `queued` con l'`outbox_id` del primo messaggio.
- `async def build_summary(session, kind: Literal["daily","weekly"], now) -> list[tuple[str, list|None]]` — finestra: `daily` dalla mezzanotte locale, `weekly` 7 giorni; contenuto: ordini eseguiti e rifiutati, eventi critici e di avviso, decisioni per tipo, la classifica in un blocco `pre`; diviso con `split_message`.
- `class SummaryScheduler(sessionmaker, *, clock=..., tz=..., tick_s=30)`, `run()` e `tick()`:
  - digest alle 07:00 (job `telegram.digest`), giornaliero alle 18:00 (job `telegram.daily`), settimanale il venerdì alle 18:30 (job `telegram.weekly`), con chiave = data locale ISO;
  - ciascuno solo se `is_ready(cfg)`, se l'ora locale è ≥ di quella prevista e < 23:00 dello stesso giorno (altrimenti log WARNING e job marcato come fatto);
  - claim con `run_job_once` nella stessa transazione dell'`enqueue` (`category` `digest|daily|weekly`, `level` info).

- [ ] **Step 1: test falliti:**
  - `test_ranking_order_and_none_last` — 4 portafogli «Test 250» con snapshot sintetici: +2%, −1%, nessuno snapshot, +0,5% → ordine atteso; il portafoglio archiviato è escluso;
  - `test_ranking_table_format` — virgola decimale, `—` per None, segno;
  - `test_daily_summary_at_1800_once` — tick alle 17:59 → niente; 18:00 → una riga in outbox `daily`; secondo tick → nessun duplicato; riavvio dello scheduler (nuova istanza) → nessun duplicato;
  - `test_weekly_only_friday_1830`;
  - `test_digest_at_0700_collects_night_events` — 2 eventi `digest` → un messaggio, righe `queued`; senza eventi → niente;
  - `test_summary_skipped_after_2300`;
  - `test_long_summary_split_with_pre_entities` — 60 portafogli → più parti ≤ 4096, ogni entità `pre` copre esattamente il blocco (Review Focus 5);
  - `test_not_ready_config_no_summary`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_telegram_summaries.py -q` → FAIL.
- [ ] **Step 3:** implementazione e collegamento nel worker.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** commit `feat(telegram): night digest, daily and weekly summaries with portfolio ranking`

### Task 6: Metodi MCP — log_decision, get_decisions, get_activity_feed, send_telegram

**Files:**
- Create: `mcp/tools_activity.py`, `mcp/tools_telegram.py`
- Modify: `mcp/server.py` (`_TOOL_MODULES`), `mcp/dto.py` (`decision_dto`)
- Test: `tests/integration/test_mcp_activity_telegram.py` (fixture `mcp_server`, `mcp_call`)

**Interfaces — Consumes:** Task 2 (`log_decision`, `list_decisions`, `list_feed`), Task 3 (`enqueue`, `agent_sent_last_minute`, `load_config`, `is_ready`, `clean_text`, `PREFIX`), `Agent.can_notify`.

**Interfaces — Produces** (tutti via `run_tool`, con audit):
- `log_decision(kind: str, summary: str, portfolio_id: str|None = None, symbols: list[str]|None = None, details: dict|None = None)` → `{decision_id, created_at}`.
- `get_decisions(portfolio_id: str|None = None, agent: str|None = None, kind: str|None = None, since: str|None = None, limit: int|str|None = 50)` → `{decisions: [{id, agent, agent_id, portfolio, portfolio_id, kind, summary, symbols, details, created_at}]}`, `limit` 1-200, dalla più recente; un `portfolio_id` senza `read` → `FORBIDDEN`.
- `get_activity_feed(after_id: int|str|None = None, since: str|None = None, limit: int|str|None = 100)` → `{items: [...], next_after_id}`, `limit` 1-200 (contratto del Task 2).
- `send_telegram(text: str, level: str = "info")` → `{outbox_id, status}`:
  - `can_notify` falso → `FORBIDDEN` «permesso notify mancante»;
  - `level` fuori da `info|warning|critical` → `INVALID_ORDER` `field=level`;
  - `clean_text(text)` vuoto o > 4096 caratteri **dopo** il prefisso → `INVALID_ORDER` `field=text`;
  - configurazione non pronta → `PROVIDER_UNAVAILABLE` «Notifiche Telegram non configurate o disattivate»;
  - con 20 messaggi già accodati negli ultimi 60 s → `RATE_LIMITED` con `details.retry_after_s`;
  - testo inviato = `PREFIX[level] + testo`; `source=f"agent:{agent_id}"`, `category="agent"`; con la pausa attiva la risposta ha `status="paused"`;
  - l'audit non registra il testo intero: in `params` va `{"level", "length"}` (il testo resta nella outbox).

- [ ] **Step 1: test falliti:**
  - `test_log_decision_and_get_decisions` (con e senza portafoglio; filtro `kind`);
  - `test_get_decisions_forbidden_portfolio`;
  - `test_feed_via_mcp_pages_like_alerts` — 5 attività, paginazione con `limit=2` fino al cursore stabile;
  - `test_feed_already_sent_for_fill`;
  - `test_send_telegram_requires_notify`;
  - `test_send_telegram_verbatim_markup` — riga in outbox con testo identico più il prefisso (Review Focus 3);
  - `test_send_telegram_rate_limit_21st` — 20 OK, il 21° `RATE_LIMITED`; 20 chiamate concorrenti con `asyncio.gather` → nessuna oltre 20;
  - `test_send_telegram_limits` — 4097 caratteri, solo spazi, `level="loud"`;
  - `test_send_telegram_not_configured`, `test_send_telegram_paused_status`;
  - `test_audit_has_no_text`.
- [ ] **Step 2:** `uv run pytest tests/integration/test_mcp_activity_telegram.py -q` → FAIL.
- [ ] **Step 3:** implementazione (docstring in inglese come gli altri tool, sintetiche e con i limiti).
- [ ] **Step 4:** PASS. `test_mcp_e2e.py` fallirà su `ALL_TOOLS` e sulla documentazione: lo aggiorna il Task 9.
- [ ] **Step 5:** commit `feat(mcp): decisions log, activity feed and send_telegram for the Notifier`

### Task 7: API admin «Notifiche», permesso notify e CLI

**Files:**
- Create: `admin/routes_telegram.py`
- Modify: `admin/routes_agents.py` (`AgentPatchIn.can_notify`, DTO con `can_notify`), `api/main.py` (`include_router`), `cli.py` (`telegram set`, `agent notify`)
- Test: `tests/integration/test_admin_telegram.py`, `tests/integration/test_admin_config.py` o `test_agents.py` (PATCH dell'agente), `tests/unit/test_cli_mappers.py` se serve

**Interfaces — Consumes:** Task 2 (`list_decisions`), Task 3 (client, config, outbox).

**Interfaces — Produces** (tutti `require_admin`, transazione `_transaction`, errori `SMTError` in italiano, `httpx.AsyncClient()` per richiesta con timeout 10 s come in `routes_connectors.py`):
- `GET /api/telegram` → `config_dto(cfg)` + `{"outbox": {"pending", "paused", "failed"}}` (conteggi).
- `PUT /api/telegram/config` body `{bot_token?: str, chat_id?: str|int, enabled?: bool}` (solo i campi presenti):
  - il token viene ripulito con strip e senza spazi;
  - con un nuovo token o un nuovo `chat_id`: `get_me()` con il token effettivo (`bot_username`) e `get_chat(chat_id)` (`chat_title`); un `TelegramError` → 422 `INVALID_ORDER` con `details.field` (`bot_token` o `chat_id`) e `e.message`, niente salvato;
  - `enabled=true` senza token o chat → 422;
  - audit `admin:telegram.config` con `params={"changed": [...], "chat_id", "enabled"}`.
- `POST /api/telegram/detect-chats` body `{bot_token?: str}` → `{chats: [{id, type, title}]}` dagli update (`message`, `my_chat_member`, `channel_post`), deduplicati per id, i più recenti per primi; solo gruppi, supergruppi e canali; errori come sopra.
- `POST /api/telegram/test` → invia subito con il client (anche in pausa) «🟢 Messaggio di prova da SMTrading» e registra una riga in outbox `category="test"` `sent` o `failed`; risposta `{ok: bool, error?: str}`; audit `admin:telegram.test`.
- `PUT /api/telegram/paused` body `{paused: bool}` → alla ripresa `resume_paused`; risposta come `GET`; audit `admin:telegram.pause`.
- `GET /api/telegram/outbox?status=&category=&before_id=&limit=50` (limit 1-200) → `{items: [{id, source, agent, category, level, text, status, attempts, last_error, created_at, sent_at}]}`.
- `POST /api/telegram/outbox/{id}/retry` → riga aggiornata; audit `admin:telegram.retry`.
- `GET /api/decisions?agent_id=&portfolio_id=&kind=&before_id=&limit=50` → `{items: [decision_dto + agent, portfolio]}`.
- `PATCH /api/agents/{id}` accetta `can_notify` (con `model_fields_set`, come `active`); audit `admin:agent.notify`; il DTO dell'agente espone `can_notify`.
- CLI:
  - `smtrading telegram set --chat-id <id> [--token-stdin] [--no-verify] [--enable]` — il token si legge solo da stdin (mai da argv); stessa verifica dell'API; stampa `@bot_username` e il titolo del gruppo, mai il token;
  - `smtrading agent notify <name> on|off`.

- [ ] **Step 1: test falliti** (respx per Telegram):
  - `test_get_config_masked` — risposta e audit non contengono il token né le sue ultime 4 cifre;
  - `test_put_token_verifies_getme_and_getchat`;
  - `test_put_bad_token_422_italian_no_token` — 401 da Telegram → 422 «Token del bot non valido», configurazione invariata, il token non è nel corpo della risposta;
  - `test_put_chat_only_keeps_token`;
  - `test_token_with_whitespace_is_stripped`;
  - `test_detect_chats_dedup_groups_only`;
  - `test_test_message_sent_even_when_paused`, `test_test_message_failure_reports_error`;
  - `test_pause_resume_moves_rows`;
  - `test_outbox_list_and_retry`;
  - `test_decisions_filters`;
  - `test_patch_agent_can_notify_audited`;
  - `test_every_change_audited` (config, pausa, retry, test);
  - CLI: `test_cli_telegram_set_reads_token_from_stdin` (il token non compare nell'output).
- [ ] **Step 2:** `uv run pytest tests/integration/test_admin_telegram.py -q` → FAIL.
- [ ] **Step 3:** implementazione.
- [ ] **Step 4:** PASS, più `uv run pytest tests/integration/test_agents.py tests/integration/test_admin_config.py -q`.
- [ ] **Step 5:** commit `feat(admin): telegram settings API, outbox, decisions, notify permission, CLI`

### Task 8: Pagina web «Notifiche» e permesso nella scheda agente

**Files:**
- Create: `web/src/pages/Notifications.tsx`, `web/src/pages/Notifications.test.tsx`, `web/src/api/hooks/telegram.ts`
- Modify: `web/src/routes.tsx` (`{ path: "notifiche", element: <Notifications /> }`), `web/src/components/Sidebar.tsx` (voce «Notifiche», icona `Bell`, dopo «Broker»), `web/src/pages/AgentDetail.tsx` (interruttore «Può inviare su Telegram (notify)»), `web/src/api/types.ts`

**Interfaces — Consumes:** il contratto HTTP del Task 7 (si può sviluppare in parallelo con dei mock MSW o i mock di fetch già usati in `Connectors.test.tsx`).

**Interfaces — Produces** (testi esatti):
- **Collegamento:**
  - campo «Token del bot» che mostra `••••` e `@bot_username`; pulsante «Sostituisci token» che apre un campo password vuoto, senza mai precompilare il token;
  - campo «ID del gruppo» con accanto il titolo del gruppo;
  - pulsante «Rileva gruppi», con l'aiuto «Scrivi `/start@<bot>` nel gruppo, poi premi Rileva gruppi»: elenco cliccabile (ID, tipo, titolo) che compila l'ID;
  - «Salva» (errori del server mostrati sotto il campo indicato da `details.field`);
  - interruttore «Notifiche attive»;
  - interruttore «Metti in pausa gli invii», con la nota «I messaggi restano in coda e partono alla ripresa»;
  - pulsante «Invia messaggio di prova» con l'esito.
- **Guida rapida** (comprimibile): creare il bot con @BotFather (`/newbot`), aggiungerlo al gruppo, scrivere `/start@<bot>`, premere «Rileva gruppi»; ore notturne 23:00-07:00 e orari dei riepiloghi.
- **Ultimi messaggi:** `DataTable` con data, origine (Sistema / nome dell'agente), tipo, livello, testo (troncato, espandibile), stato (badge «In coda», «Inviato», «Fallito», «In pausa»), tentativi, errore; pulsante «Riprova» sulle righe `failed`; filtro per stato.
- **Decisioni:** `DataTable` filtrabile per agente, portafoglio e tipo (etichette italiane: Proposta, Approvazione, Veto, Allocazione, Strategia, Regola di rischio, Altro); «Carica altre» con `before_id`.
- Aggiornamento sull'evento WS `notification` già esistente (invalida la query della outbox) e ogni 15 s.

- [ ] **Step 1: test falliti** (`Notifications.test.tsx`, Vitest + Testing Library):
  - `renders masked token and bot name, never the token`;
  - `replace token sends bot_token only when typed`;
  - `detect groups fills chat id on click`;
  - `server error shown under the field`;
  - `retry button only on failed rows and calls retry`;
  - `pause toggle calls PUT /api/telegram/paused`;
  - `decisions filter by kind`;
  - `AgentDetail` toggle calls `PATCH { can_notify }`.
- [ ] **Step 2:** `npm --prefix web test -- --run src/pages/Notifications.test.tsx` → FAIL.
- [ ] **Step 3:** implementazione con i componenti esistenti (`PageHeader`, `DataTable`, `FormField`, `ConfirmDialog`, `ui/*`).
- [ ] **Step 4:** PASS; `npm --prefix web run lint` e `npm --prefix web run build` puliti.
- [ ] **Step 5:** commit `feat(web): Notifiche page and notify permission toggle`

### Task 9: Documentazione, e2e MCP e verifica finale

**Files:**
- Modify: `docs/paperclip-mcp.md`, `backend/tests/integration/test_mcp_e2e.py` (`ALL_TOOLS` + gruppo `# activity and telegram`), `README.md` (sezione «Notifiche Telegram»), `backend/src/smtrading/mcp/smoke.py` (se conta i metodi)

**Contenuto di `docs/paperclip-mcp.md`:**
- Elenco dei metodi con le righe `` - `log_decision`: ... ``, `` - `get_decisions`: ... ``, `` - `get_activity_feed`: ... ``, `` - `send_telegram`: ... `` (formato riconosciuto da `test_docs_list_every_registered_tool`).
- Sezione «Decisioni»: i tipi (`proposal`, `approval`, `veto`, `allocation`, `strategy`, `risk_rule`, `other`), i limiti e la regola: ogni agente registra con `log_decision` ogni decisione presa in Paperclip, poi coinvolge il Notifier con un task o una menzione.
- Sezione «Feed attività»: contratto del cursore (come «Polling degli alert»), finestra di assestamento di 3 s, campo `already_sent` («SMTrading lo invia già da sé: non ripeterlo»), elenco dei tipi.
- Sezione «Notifiche Telegram»: cosa invia SMTrading da sé (tabella dei livelli del Task 2), ore notturne, riepiloghi 07:00/18:00/venerdì 18:30, limiti di `send_telegram` (permesso `notify`, 20/min, 4096, testo semplice), errori `RATE_LIMITED` e `PROVIDER_UNAVAILABLE`.
- Sezione «Prompt suggerito per il Notifier» (blocco di testo da incollare in Paperclip): lavora solo su richiesta, senza polling; per ogni richiesta legge `get_activity_feed` dal proprio ultimo cursore e `get_decisions`, non ripete gli elementi con `already_sent: true`, scrive messaggi brevi in italiano (al massimo 10 righe, nessun markup, numeri con la virgola), usa `level=critical` solo per rischi reali, raggruppa più decisioni in un messaggio, alle 18:00 e il venerdì può aggiungere un commento alle decisioni del giorno dopo il riepilogo di SMTrading, rispetta i 20 messaggi al minuto e non invia mai token, ID o dati personali.

- [ ] **Step 1:** aggiorna `ALL_TOOLS` → `uv run pytest tests/integration/test_mcp_e2e.py -q` → FAIL su `test_docs_list_every_registered_tool` finché la documentazione non è aggiornata.
- [ ] **Step 2:** documentazione e README → PASS.
- [ ] **Step 3: verifica:**
  1. `cd backend && uv run pytest -q` → tutto verde;
  2. `npm --prefix web test -- --run` → verde;
  3. `docker compose up -d --build`, poi `alembic current` = head;
  4. lo smoke MCP esce con 0 ed elenca 4 metodi in più;
  5. `bash scripts/e2e.sh` → verde;
  6. `docker compose logs api worker | grep -E '[0-9]{6,12}:[A-Za-z0-9_-]{30,}'` → nessun risultato.
- [ ] **Step 4:** commit `docs: paperclip MCP guide for decisions, activity feed, telegram and the Notifier prompt`
- [ ] **Step 5 (controller, non l'implementer):**
  - carica bot e gruppo dal file privato dell'utente con `docker compose exec -T api smtrading telegram set --chat-id <id> --token-stdin --enable < token`;
  - abilita `smtrading agent notify Notifier on` (il token dell'agente non cambia);
  - premi «Invia messaggio di prova» dalla pagina Notifiche;
  - annota l'esito nella memoria di progetto.
