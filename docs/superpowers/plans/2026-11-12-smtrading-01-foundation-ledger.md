# SMTrading — Piano 1: Fondamenta, Registro, Portafogli, Commissioni

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend Python avviabile in Docker con database migrato, registro a partita doppia immutabile (catena di hash), anagrafica strumenti, portafogli con depositi e prelievi, calcolo e risoluzione delle commissioni, seed iniziale.

**Architecture:** Un unico pacchetto `smtrading` (src layout) con moduli di dominio (`ledger`, `instruments`, `portfolios`, `fees`) che lavorano su una `AsyncSession` SQLAlchemy. **I servizi non fanno mai commit**: il commit lo fa chi li chiama (handler API, job, seed). Due processi (`api` FastAPI e `worker`) condividono la stessa immagine. Postgres 16 e Redis 7 girano via Docker Compose sulla rete `smtrading-net`.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy 2.0 async + asyncpg, Alembic, pydantic-settings, redis-py (asyncio), pytest + pytest-asyncio + Hypothesis + testcontainers, ruff.

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§3, §4, §7, §11, §12, §13)

### Roadmap dei piani
1. **Fondamenta + Registro + Portafogli + Commissioni** (questo piano)
2. Market data: connettori gratuiti, router, cache, streaming, indicatori
3. Motore ordini + rischio + margine + interessi, dividendi e split
4. Server MCP (auth, metodi, audit) + collegamento di Paperclip
5. API admin + web app React
6. Connettori con chiave, broker paper, alert, watchlist, screener, backtest

## Global Constraints

- Python 3.12; dipendenze gestite con `uv` (`backend/pyproject.toml`, `uv.lock` committato).
- Importi e quantità: sempre `Decimal`, mai `float`; nel database `NUMERIC(38,18)`.
- Valuta base dei portafogli: `EUR`. Fuso di default: `Europe/Rome`.
- Commissione globale di default: fissa 1 EUR + 0,1%, minimo 1 EUR, massimo 20 EUR; cambio 0,25%.
- Portafoglio seed: "Demo", 100.000 EUR.
- Percentuali delle commissioni salvate **in punti percentuali** (`Decimal("0.1")` = 0,1%).
- Arrotondamento degli importi in valuta: `ROUND_HALF_UP` alle unità minori (JPY 0 decimali, tutte le altre fiat 2); le quantità di titoli e crypto non vengono mai arrotondate dal registro.
- Rete Docker con nome fisso `smtrading-net`; porta host `8000` per `api`.
- I servizi di dominio ricevono `session: AsyncSession` come primo argomento e non chiamano mai `commit()`.
- Errori di dominio: sempre `SMTError(code, message, details)` con `code` preso da `ErrorCode`.
- I test di integrazione richiedono Docker raggiungibile da WSL (testcontainers).

## Review Focus

1. **Journal scritti in contemporanea da portafogli diversi** → la catena di hash resta lineare (nessun `prev_hash` duplicato). Test in Task 5.
2. **Precisione decimale**: una quantità crypto `0.123456789012345678` si salva e si rilegge identica; commissioni in JPY arrotondate a 0 decimali. Test in Task 4 e Task 8.
3. **Importi zero o negativi** in deposito o prelievo → `INVALID_AMOUNT`, nessun journal creato. Test in Task 7.
4. **Storno di un journal già stornato, o di uno storno** → `ALREADY_REVERSED` / `INVALID_REVERSAL`; uno storno che renderebbe il cash negativo → `INSUFFICIENT_FUNDS`. Test in Task 6.
5. **Seed eseguito due volte** (riavvio del container) → nessun portafoglio o deposito duplicato. Test in Task 10.

---

## File Structure

```
docker-compose.yml
.env.example
backend/
  Dockerfile
  pyproject.toml
  alembic.ini
  docker/entrypoint-api.sh
  migrations/env.py
  migrations/versions/0001_initial.py
  src/smtrading/
    __init__.py
    common/config.py        # Settings (pydantic-settings)
    common/db.py            # engine, sessionmaker, Base
    common/errors.py        # ErrorCode, SMTError
    common/money.py         # quantize_money, decimal_to_str
    ledger/models.py        # Account, Journal, JournalLine, AccountBalance, enum
    ledger/hashing.py       # payload canonico + sha256
    ledger/service.py       # post_journal, reverse_journal, verify_*
    instruments/models.py   # Instrument, AssetClass
    instruments/service.py  # canonical_symbol, get_instrument, validate_*
    portfolios/models.py    # Portfolio
    portfolios/service.py   # create_portfolio, deposit, withdraw, get_cash_balances
    fees/models.py          # FeeRule (ORM)
    fees/calculator.py      # FeeRuleData, Tier, compute_trade_fee, compute_fx_fee
    fees/resolver.py        # resolve_rule, load_rules, quote_fee
    seed.py                 # run_seed (idempotente)
    api/main.py             # FastAPI app, /health
    worker/main.py          # loop di heartbeat
  tests/
    conftest.py             # container Postgres/Redis, migrazioni, db_session con rollback
    unit/test_errors_config.py
    unit/test_money.py
    unit/test_hashing.py
    unit/test_fee_calculator.py
    unit/test_fee_resolver.py
    integration/test_health.py
    integration/test_migrations.py
    integration/test_ledger.py
    integration/test_ledger_concurrency.py
    integration/test_instruments.py
    integration/test_portfolios.py
    integration/test_fees_db.py
    integration/test_seed.py
```

---

### Task 1: Scheletro del progetto, configurazione, errori, money

**Files:**
- Create: `backend/pyproject.toml`, `backend/src/smtrading/__init__.py`, `backend/src/smtrading/common/{__init__,config,errors,money}.py`, `.gitignore`
- Test: `backend/tests/unit/test_errors_config.py`, `backend/tests/unit/test_money.py`

**Interfaces:**
- Produces:
  - `Settings(BaseSettings)` con prefisso env `SMT_`: `database_url: str`, `redis_url: str`, `master_key: str`, `tz: str = "Europe/Rome"`, `admin_email: str`, `admin_password: str`; `get_settings() -> Settings` (cache con lru_cache).
  - `class ErrorCode(StrEnum)`: tutti i codici della spec §9, più `INSUFFICIENT_FUNDS`, `INVALID_AMOUNT`, `LEDGER_UNBALANCED`, `LEDGER_INVALID`, `ALREADY_REVERSED`, `INVALID_REVERSAL`, `FEE_RULE_NOT_FOUND`.
  - `class SMTError(Exception)`: `__init__(self, code: ErrorCode, message: str, details: dict | None = None)`, attributi omonimi; `to_dict() -> {"code","message","details"}`.
  - `quantize_money(amount: Decimal, currency: str) -> Decimal` (JPY→0 decimali, altre→2, ROUND_HALF_UP).
  - `decimal_to_str(d: Decimal) -> str`: forma normalizzata, senza esponente e senza zeri finali (`Decimal("1.500")→"1.5"`, `Decimal("0E-18")→"0"`, `Decimal("100")→"100"`).

- [ ] **Step 1: Crea `pyproject.toml`** con il progetto `smtrading`, src layout, dipendenze di runtime (fastapi, uvicorn[standard], sqlalchemy[asyncio]>=2.0, asyncpg, alembic, pydantic-settings, redis>=5) e di dev (pytest, pytest-asyncio, hypothesis, testcontainers[postgres,redis], httpx, ruff). `[tool.pytest.ini_options] asyncio_mode="auto"`, `testpaths=["tests"]`. Poi `cd backend && uv sync`.
- [ ] **Step 2: Scrivi i test che falliscono**

```python
# tests/unit/test_errors_config.py
def test_smterror_to_dict():
    e = SMTError(ErrorCode.INSUFFICIENT_FUNDS, "no cash", {"need": "10"})
    assert e.to_dict() == {"code": "INSUFFICIENT_FUNDS", "message": "no cash", "details": {"need": "10"}}

def test_settings_from_env(monkeypatch):
    for k, v in {"SMT_DATABASE_URL": "postgresql+asyncpg://x", "SMT_REDIS_URL": "redis://r",
                 "SMT_MASTER_KEY": "k", "SMT_ADMIN_EMAIL": "a@b.c", "SMT_ADMIN_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url == "postgresql+asyncpg://x" and s.tz == "Europe/Rome"

# tests/unit/test_money.py
@pytest.mark.parametrize("amt,ccy,exp", [("1.005","EUR","1.01"), ("1.004","USD","1.00"), ("150.5","JPY","151")])
def test_quantize_money(amt, ccy, exp):
    assert quantize_money(Decimal(amt), ccy) == Decimal(exp)

@pytest.mark.parametrize("d,exp", [("1.500","1.5"), ("0E-18","0"), ("100","100"), ("0.000000010","0.00000001")])
def test_decimal_to_str(d, exp):
    assert decimal_to_str(Decimal(d)) == exp
```

- [ ] **Step 3: Esegui** `cd backend && uv run pytest tests/unit -v` → FAIL (import error).
- [ ] **Step 4: Implementa** `config.py`, `errors.py`, `money.py` con le firme indicate sopra.
- [ ] **Step 5: Esegui** `uv run pytest tests/unit -v && uv run ruff check .` → tutto PASS.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: project skeleton, settings, errors, money helpers"`

---

### Task 2: Docker Compose, API `/health`, heartbeat del worker, base dei test

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `backend/Dockerfile`, `backend/docker/entrypoint-api.sh`, `backend/src/smtrading/common/db.py`, `backend/src/smtrading/api/main.py`, `backend/src/smtrading/worker/main.py`, `backend/tests/conftest.py`
- Test: `backend/tests/integration/test_health.py`

**Interfaces:**
- Consumes: `get_settings()` (Task 1).
- Produces:
  - `db.py`: `class Base(DeclarativeBase)`; `make_engine(url: str) -> AsyncEngine`; `get_sessionmaker() -> async_sessionmaker[AsyncSession]`; dipendenza FastAPI `get_session() -> AsyncIterator[AsyncSession]`.
  - `api/main.py`: `create_app() -> FastAPI`; `GET /health` → `200 {"status": "ok"|"degraded", "db": bool, "redis": bool, "worker_heartbeat_age_s": float | None}`.
  - `worker/main.py`: `async def run()`, che scrive `SET worker:heartbeat <unix_ts>` ogni 10s; avvio con `python -m smtrading.worker.main`.
  - Fixture in `conftest.py`: `pg_url` (session, `PostgresContainer("postgres:16")`, URL convertita in `postgresql+asyncpg`), `redis_url` (session, `RedisContainer("redis:7")`), `migrated_db` (session, `alembic upgrade head` sulla `pg_url`), `db_session` (funzione: connessione → `begin()` → `AsyncSession(bind=conn, join_transaction_mode="create_savepoint")` → rollback alla fine), `app_client` (httpx `AsyncClient` con `ASGITransport` su `create_app()`, env impostate sui container).
- Compose: servizi `postgres` (volume `pgdata`, healthcheck `pg_isready`), `redis`, `api` (build `./backend`, porta `8000:8000`, `depends_on` con healthy, entrypoint `entrypoint-api.sh`), `worker` (stessa immagine, `command: python -m smtrading.worker.main`). Rete `smtrading-net` con `name: smtrading-net`. Variabili da `.env`.
- `entrypoint-api.sh`: `alembic upgrade head && python -m smtrading.seed && exec uvicorn smtrading.api.main:create_app --factory --host 0.0.0.0 --port 8000`. Finché seed e migrazioni non esistono (Task 3 e 10), lo script deve tollerarne l'assenza: aggiungili quando arrivano quei task.

- [ ] **Step 1: Scrivi il test che fallisce**

```python
# tests/integration/test_health.py
async def test_health_ok(app_client):
    r = await app_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is True and body["redis"] is True
    assert body["status"] in ("ok", "degraded")  # degraded: nessun heartbeat del worker nel test
```

- [ ] **Step 2: Esegui** `uv run pytest tests/integration/test_health.py -v` → FAIL.
- [ ] **Step 3: Implementa** `db.py`, `api/main.py`, `worker/main.py`, `conftest.py`. `status` = `"ok"` se db e redis rispondono e l'età dell'heartbeat è < 30s, altrimenti `"degraded"`.
- [ ] **Step 4: Esegui** il test → PASS.
- [ ] **Step 5: Scrivi** `Dockerfile` (python:3.12-slim, uv, `uv sync --frozen --no-dev`, utente non root), `docker-compose.yml`, `.env.example` (`SMT_MASTER_KEY=`, `SMT_ADMIN_EMAIL=`, `SMT_ADMIN_PASSWORD=`, `POSTGRES_PASSWORD=`, `SMT_TZ=Europe/Rome`).
- [ ] **Step 6: Verifica manuale** `cp .env.example .env` (compila i valori) → `docker compose up -d --build` → `curl -s localhost:8000/health` → `"status":"ok"` entro 30s; `docker network ls | grep smtrading-net` → presente.
- [ ] **Step 7: Commit** `git commit -m "feat: docker compose, api health, worker heartbeat, test fixtures"`

---

### Task 3: Modelli e migrazione iniziale con trigger di immutabilità

**Files:**
- Create: `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/versions/0001_initial.py`, `backend/src/smtrading/{ledger,instruments,portfolios,fees}/models.py`
- Test: `backend/tests/integration/test_migrations.py`

**Interfaces:**
- Produces (ORM, PK `UUID` default `uuid4` salvo dove indicato):
  - `AccountKind(StrEnum)`: `cash, position, collateral, margin_loan, system_equity, system_fees, system_fx, system_interest`.
  - `JournalType(StrEnum)`: `DEPOSIT, WITHDRAWAL, TRADE, FEE, FX, INTEREST, DIVIDEND, CORPORATE_ACTION, LIQUIDATION, RESET, REVERSAL`.
  - `Account`: `id, portfolio_id: UUID|None (FK portfolios), asset: str, kind: AccountKind, created_at`; `UNIQUE NULLS NOT DISTINCT (portfolio_id, asset, kind)`.
  - `Journal`: `id, seq: BIGINT identity UNIQUE, type, order_id: UUID|None, fill_id: UUID|None, reversed_journal_id: UUID|None (FK journals, UNIQUE), author_type: str ('agent'|'admin'|'system'), author_id: str|None, reason: str|None, created_at timestamptz, prev_hash: char(64), hash: char(64) UNIQUE`.
  - `JournalLine`: `id BIGINT identity, journal_id FK, account_id FK, quantity NUMERIC(38,18), unit_price NUMERIC(38,18)|None, price_ccy str|None`.
  - `AccountBalance`: `account_id PK FK, balance NUMERIC(38,18), updated_at`.
  - `AssetClass(StrEnum)`: `us_equity, eu_equity, crypto, forex` (gli ETF ricadono in us_equity/eu_equity).
  - `Instrument`: `symbol PK str, name, asset_class, market str, currency str, lot_size NUMERIC, tick_size NUMERIC, shortable bool, active bool, provider_symbols JSONB`.
  - `PortfolioStatus(StrEnum)`: `active, reduce_only, locked`.
  - `Portfolio`: `id, name UNIQUE, base_currency='EUR', leverage NUMERIC=1, maintenance_margin_pct NUMERIC=50, warning_margin_pct NUMERIC=75, slippage_bps int=0, status, timezone='Europe/Rome', created_at`.
  - `FeeScope(StrEnum)`: `global, asset_class, portfolio`; `FeeSide(StrEnum)`: `buy, sell, both`.
  - `FeeRule`: `id, name, scope, scope_ref str|None, side, currency, fixed, pct, min_fee|None, max_fee|None, tiers JSONB|None, fx_pct, priority int=0, active bool, created_at`.
- La migrazione crea una funzione `smt_block_mutation()` che fa `RAISE EXCEPTION 'LEDGER_IMMUTABLE'`, e i trigger `BEFORE UPDATE OR DELETE ... FOR EACH ROW` e `BEFORE TRUNCATE ... FOR EACH STATEMENT` su `journals` e `journal_lines`.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
# tests/integration/test_migrations.py
async def test_tables_exist(db_session):
    names = {r[0] for r in (await db_session.execute(text(
        "select tablename from pg_tables where schemaname='public'"))).all()}
    assert {"accounts","journals","journal_lines","account_balances","instruments","portfolios","fee_rules"} <= names

@pytest.mark.parametrize("stmt", [
    "update journals set reason='x'", "delete from journal_lines", "truncate journals cascade"])
async def test_ledger_tables_immutable(db_session, stmt, posted_journal):
    with pytest.raises(DBAPIError, match="LEDGER_IMMUTABLE"):
        await db_session.execute(text(stmt))
```
`posted_journal` è una fixture che inserisce via SQL grezzo un portafoglio, due account, un journal e due righe, così il trigger ha righe su cui scattare. Il test di TRUNCATE non ne ha bisogno ma la riusa.

- [ ] **Step 2: Esegui** `uv run pytest tests/integration/test_migrations.py -v` → FAIL.
- [ ] **Step 3: Implementa** i modelli, `env.py` (async, `target_metadata = Base.metadata`, URL da `get_settings()` o dall'opzione `-x url=`) e la `0001_initial.py` scritta a mano, trigger compresi.
- [ ] **Step 4: Esegui** il test → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: initial schema with immutable ledger triggers"`

---

### Task 4: Hash canonico del journal

**Files:**
- Create: `backend/src/smtrading/ledger/hashing.py`
- Test: `backend/tests/unit/test_hashing.py`

**Interfaces:**
- Produces:
  - `GENESIS_HASH = "0" * 64`
  - `canonical_payload(*, journal_id: UUID, type: str, created_at: datetime, author_type: str, author_id: str|None, reason: str|None, order_id: UUID|None, fill_id: UUID|None, reversed_journal_id: UUID|None, lines: list[tuple[UUID, Decimal]]) -> bytes`: JSON con chiavi ordinate, senza spazi, UUID come stringhe, `created_at` in ISO 8601 UTC con microsecondi, `lines` ordinate per `(str(account_id), decimal_to_str(qty))` e quantità serializzate con `decimal_to_str`.
  - `compute_hash(prev_hash: str, payload: bytes) -> str` = `sha256(prev_hash.encode() + payload).hexdigest()`.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
def test_payload_independent_of_line_order_and_trailing_zeros():
    a, b = uuid4(), uuid4()
    base = dict(journal_id=J, type="DEPOSIT", created_at=TS, author_type="admin", author_id="1",
                reason=None, order_id=None, fill_id=None, reversed_journal_id=None)
    p1 = canonical_payload(**base, lines=[(a, Decimal("10.0")), (b, Decimal("-10"))])
    p2 = canonical_payload(**base, lines=[(b, Decimal("-10.000")), (a, Decimal("10"))])
    assert p1 == p2

def test_hash_changes_with_prev_and_content():
    p = canonical_payload(**BASE, lines=LINES)
    assert compute_hash(GENESIS_HASH, p) != compute_hash("1"*64, p)
    assert len(compute_hash(GENESIS_HASH, p)) == 64

def test_high_precision_quantity_preserved():
    p = canonical_payload(**BASE, lines=[(A, Decimal("0.123456789012345678")), (B, Decimal("-0.123456789012345678"))])
    assert b"0.123456789012345678" in p
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa.** **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: canonical journal hashing"`

---

### Task 5: `post_journal`: registrazione con bilanciamento, saldi e catena

**Files:**
- Create: `backend/src/smtrading/ledger/service.py`
- Test: `backend/tests/integration/test_ledger.py`, `backend/tests/integration/test_ledger_concurrency.py`

**Interfaces:**
- Consumes: modelli (Task 3), `canonical_payload`/`compute_hash`/`GENESIS_HASH` (Task 4), `SMTError`.
- Produces:
  - `@dataclass(frozen=True) class LineSpec: account_id: UUID; quantity: Decimal; unit_price: Decimal | None = None; price_ccy: str | None = None`
  - `async def get_or_create_account(session, *, portfolio_id: UUID | None, asset: str, kind: AccountKind) -> Account`, con `INSERT ... ON CONFLICT DO NOTHING` seguito da `SELECT`.
  - `async def post_journal(session, *, type: JournalType, lines: list[LineSpec], author_type: str, author_id: str | None, reason: str | None = None, order_id: UUID | None = None, fill_id: UUID | None = None, reversed_journal_id: UUID | None = None, allow_negative: frozenset[AccountKind] = frozenset()) -> Journal`
  - `async def get_balance(session, account_id: UUID) -> Decimal` (0 se non esiste un saldo).
- Regole:
  1. Almeno 2 righe e nessuna quantità a zero, altrimenti `LEDGER_INVALID`.
  2. Per ogni `asset` (letto dagli account) la somma è 0, altrimenti `LEDGER_UNBALANCED`.
  3. `SELECT pg_advisory_xact_lock(7300001)` prima di leggere l'ultimo hash (`ORDER BY seq DESC LIMIT 1`; se non c'è nessun journal → `GENESIS_HASH`).
  4. Insert del journal e delle righe; upsert di `account_balances` (`balance = balance + qty`).
  5. Dopo l'aggiornamento, un account con `portfolio_id` non nullo, `kind ∉ allow_negative`, `kind ∈ {cash, position, collateral}` e saldo < 0 → `INSUFFICIENT_FUNDS` con `details={"account_id","asset","balance"}`. Lo rileva il servizio prima di uscire; la transazione la annulla chi chiama.
  6. `created_at` è generato in Python (UTC, con microsecondi) e quindi è lo stesso valore usato nell'hash.

- [ ] **Step 1: Scrivi i test che falliscono** (`test_ledger.py`)

```python
async def test_balanced_deposit_updates_balances(db_session, portfolio):
    cash = await get_or_create_account(db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.cash)
    eq = await get_or_create_account(db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity)
    j = await post_journal(db_session, type=JournalType.DEPOSIT, author_type="admin", author_id="1",
                           lines=[LineSpec(cash.id, Decimal("1000")), LineSpec(eq.id, Decimal("-1000"))])
    assert await get_balance(db_session, cash.id) == Decimal("1000")
    assert j.prev_hash == GENESIS_HASH and len(j.hash) == 64

async def test_unbalanced_rejected(db_session, cash_and_equity):
    with pytest.raises(SMTError) as e:
        await post_journal(db_session, type=JournalType.DEPOSIT, author_type="admin", author_id="1",
                           lines=[LineSpec(CASH, Decimal("10")), LineSpec(EQ, Decimal("-9"))])
    assert e.value.code == ErrorCode.LEDGER_UNBALANCED

async def test_single_or_zero_line_rejected(...)          # → LEDGER_INVALID
async def test_negative_cash_rejected(...)               # prelievo oltre il saldo → INSUFFICIENT_FUNDS
async def test_system_account_may_go_negative(...)       # system_equity a -1000 è ammesso
async def test_allow_negative_position(...)              # allow_negative={position} → ammesso
async def test_chain_links(...)                          # il secondo journal ha prev_hash == hash del primo
async def test_get_or_create_account_idempotent(...)     # due chiamate → stesso id
```
`portfolio` è una fixture che inserisce un `Portfolio(name="T")` nella `db_session`.

Property-based, nello stesso file:
```python
@given(st.lists(st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("1e9"), places=6), min_size=1, max_size=10))
async def test_random_deposits_balance_equals_sum(amounts): ...
# per ogni importo: DEPOSIT su cash; alla fine il saldo cash == sum(amounts) e system_equity == -sum
```
(Usa `@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])` e un savepoint per esempio.)

- [ ] **Step 2: Esegui** `uv run pytest tests/integration/test_ledger.py -v` → FAIL.
- [ ] **Step 3: Implementa** `service.py` secondo le regole qui sopra.
- [ ] **Step 4: Esegui** → PASS.
- [ ] **Step 5: Scrivi il test di concorrenza** (`test_ledger_concurrency.py`). Niente fixture con rollback: due sessioni reali sul `migrated_db`, ognuna con il suo portafoglio, 20 depositi ciascuna lanciati in parallelo con `asyncio.gather`, ogni deposito committato. Assert: `count(distinct prev_hash) == count(*)` sui journal del test e la catena è percorribile per `seq`. Pulizia: crea un database dedicato `CREATE DATABASE t_conc TEMPLATE ...` oppure usa un container separato in questa fixture. Le tabelle sono immutabili, quindi non si possono ripulire.
- [ ] **Step 6: Esegui** → PASS.
- [ ] **Step 7: Commit** `git commit -m "feat: double-entry post_journal with hash chain and balance checks"`

---

### Task 6: Storno e verifiche (catena e saldi)

**Files:**
- Modify: `backend/src/smtrading/ledger/service.py`
- Test: `backend/tests/integration/test_ledger.py` (aggiunte)

**Interfaces:**
- Produces:
  - `async def reverse_journal(session, journal_id: UUID, *, reason: str, author_type: str, author_id: str | None) -> Journal`: righe con quantità invertita, `type=REVERSAL`, `reversed_journal_id=journal_id`. Errori: `reason` vuota → `INVALID_REVERSAL`; il journal è già di tipo `REVERSAL` → `INVALID_REVERSAL`; esiste già uno storno → `ALREADY_REVERSED`; journal inesistente → `NOT_FOUND`. Valgono gli stessi controlli di `post_journal` sui saldi negativi.
  - `@dataclass class ChainReport: ok: bool; checked: int; first_bad_journal_id: UUID | None`
  - `async def verify_chain(session) -> ChainReport`: ricalcola gli hash in ordine di `seq`.
  - `async def verify_balances(session) -> list[UUID]`: gli account il cui saldo materializzato è diverso dalla somma delle righe.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
async def test_reverse_restores_balance(...)          # deposito 100 → storno → saldo 0; type REVERSAL
async def test_reverse_twice_rejected(...)            # → ALREADY_REVERSED
async def test_reverse_a_reversal_rejected(...)       # → INVALID_REVERSAL
async def test_reverse_requires_reason(...)           # reason="  " → INVALID_REVERSAL
async def test_reverse_that_overdraws_rejected(...)   # deposito 100, prelievo 80, storno del deposito → INSUFFICIENT_FUNDS
async def test_verify_chain_ok_and_detects_tamper(...)
    # ok su una catena valida; poi in una connessione separata disabilita il trigger come superuser
    # (ALTER TABLE journals DISABLE TRIGGER ALL), modifica reason, riattiva il trigger → ok False, first_bad == quel journal
async def test_verify_balances_empty_when_consistent(...)
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa.** **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: journal reversal and ledger verification"`

---

### Task 7: Strumenti e portafogli (creazione, deposito, prelievo)

**Files:**
- Create: `backend/src/smtrading/instruments/service.py`, `backend/src/smtrading/portfolios/service.py`
- Test: `backend/tests/integration/test_instruments.py`, `backend/tests/integration/test_portfolios.py`

**Interfaces:**
- Consumes: `post_journal`, `get_or_create_account`, `get_balance` (Task 5).
- Produces:
  - `canonical_symbol(raw: str) -> str`: strip + upper.
  - `async def get_instrument(session, symbol: str) -> Instrument`: `SYMBOL_NOT_FOUND` se manca o non è attivo.
  - `validate_quantity(instr: Instrument, qty: Decimal) -> None`: `INVALID_ORDER` se `qty <= 0` o non è multiplo di `lot_size`.
  - `validate_price(instr: Instrument, price: Decimal) -> None`: `INVALID_ORDER` se `price <= 0` o non è multiplo di `tick_size`.
  - `async def create_portfolio(session, *, name: str, initial_cash: Decimal, base_currency: str = "EUR", leverage: Decimal = Decimal("1"), slippage_bps: int = 0, timezone: str = "Europe/Rome", author_id: str | None = None) -> Portfolio`: se `initial_cash > 0` registra un `DEPOSIT` (cash ↔ `system_equity`, asset = base_currency). `initial_cash < 0` → `INVALID_AMOUNT`.
  - `async def deposit(session, portfolio_id: UUID, amount: Decimal, currency: str, *, author_id: str | None, reason: str | None = None) -> Journal`
  - `async def withdraw(session, portfolio_id: UUID, amount: Decimal, currency: str, *, author_id: str | None, reason: str | None = None) -> Journal`: cash ↔ `system_equity`, `type=WITHDRAWAL`.
  - Per `deposit` e `withdraw`: `amount <= 0` → `INVALID_AMOUNT`; portafoglio inesistente → `NOT_FOUND`; importo arrotondato con `quantize_money`.
  - `async def get_cash_balances(session, portfolio_id: UUID) -> dict[str, Decimal]`: solo gli account `cash`, valuta → saldo.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
async def test_create_portfolio_with_initial_cash(db_session):
    p = await create_portfolio(db_session, name="Demo", initial_cash=Decimal("100000"))
    assert await get_cash_balances(db_session, p.id) == {"EUR": Decimal("100000")}

@pytest.mark.parametrize("amt", ["0", "-5"])
async def test_deposit_invalid_amount(db_session, portfolio, amt):   # → INVALID_AMOUNT, nessun journal creato
async def test_deposit_other_currency_creates_usd_account(...)       # deposito di 500 USD → {"EUR":..., "USD": 500}
async def test_withdraw_more_than_balance(...)                       # → INSUFFICIENT_FUNDS
async def test_deposit_rounds_to_minor_units(...)                    # 10.005 EUR → 10.01
async def test_unknown_portfolio(...)                                # → NOT_FOUND

# test_instruments.py
async def test_get_instrument_case_insensitive(...)   # " aapl " → AAPL
async def test_unknown_symbol(...)                    # → SYMBOL_NOT_FOUND
@pytest.mark.parametrize("qty,ok", [("1",True),("0.5",False),("0",False)])   # lot_size 1
def test_validate_quantity(qty, ok): ...
def test_validate_crypto_quantity_8_decimals(...)     # lot 0.00000001: "0.12345678" ok, "0.123456789" → INVALID_ORDER
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa.** **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: instruments lookup/validation and portfolio cash operations"`

---

### Task 8: Calcolatore delle commissioni (puro)

**Files:**
- Create: `backend/src/smtrading/fees/calculator.py`
- Test: `backend/tests/unit/test_fee_calculator.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Tier: up_to: Decimal | None; pct: Decimal`
  - `@dataclass(frozen=True) class FeeRuleData: id: UUID; scope: FeeScope; scope_ref: str | None; side: FeeSide; currency: str; fixed: Decimal; pct: Decimal; min_fee: Decimal | None; max_fee: Decimal | None; tiers: tuple[Tier, ...] | None; fx_pct: Decimal; priority: int; active: bool; created_at: datetime`
  - `compute_trade_fee(rule: FeeRuleData, notional: Decimal) -> Decimal`: `notional` nella valuta della regola. Se ci sono scaglioni, `pct` = quella del primo scaglione con `notional <= up_to` (oppure `up_to is None`), applicata all'**intero** controvalore. Formula `fixed + notional*pct/100`, poi `max(min_fee)` e `min(max_fee)`, poi `quantize_money(…, rule.currency)`. `notional < 0` → `INVALID_AMOUNT`.
  - `compute_fx_fee(rule: FeeRuleData, converted_amount: Decimal, currency: str) -> Decimal` = `quantize_money(abs(converted_amount) * fx_pct / 100, currency)`.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
DEFAULT = rule(fixed="1", pct="0.1", min_fee="1", max_fee="20", fx_pct="0.25", currency="EUR")

@pytest.mark.parametrize("notional,exp", [("500","1.50"), ("10","1.01"), ("0","1.00"), ("50000","20.00")])
def test_default_rule(notional, exp):
    assert compute_trade_fee(DEFAULT, Decimal(notional)) == Decimal(exp)

TIERED = rule(fixed="0", pct="0", tiers=((D("1000"),D("0.2")),(D("10000"),D("0.1")),(None,D("0.05"))))
@pytest.mark.parametrize("notional,exp", [("1000","2.00"), ("5000","5.00"), ("20000","10.00")])
def test_tiers(notional, exp): ...

def test_fx_fee():
    assert compute_fx_fee(DEFAULT, Decimal("-1900"), "USD") == Decimal("4.75")

def test_jpy_rounding():
    r = rule(fixed="0", pct="0.1", currency="JPY")
    assert compute_trade_fee(r, Decimal("15050")) == Decimal("15")

def test_negative_notional_rejected(): ...   # → INVALID_AMOUNT
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa.** **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: fee calculator (fixed+pct, min/max, tiers, fx)"`

---

### Task 9: Risoluzione delle regole e fee quote dal database

**Files:**
- Create: `backend/src/smtrading/fees/resolver.py`
- Test: `backend/tests/unit/test_fee_resolver.py`, `backend/tests/integration/test_fees_db.py`

**Interfaces:**
- Consumes: `FeeRuleData`, `compute_trade_fee` (Task 8), `FeeRule` ORM (Task 3).
- Produces:
  - `resolve_rule(rules: Sequence[FeeRuleData], *, portfolio_id: UUID, asset_class: AssetClass, side: Literal["buy","sell"]) -> FeeRuleData`: considera solo le regole attive con `side ∈ {side, both}` e la portata corrispondente (`portfolio` con `scope_ref == str(portfolio_id)`, `asset_class` con `scope_ref == asset_class.value`, `global`). Ordina per specificità (portfolio=2, asset_class=1, global=0) decrescente, poi `priority` decrescente, poi `created_at` più recente. Nessuna regola → `FEE_RULE_NOT_FOUND`.
  - `async def load_rules(session) -> list[FeeRuleData]`
  - `@dataclass class FeeQuote: rule_id: UUID; amount: Decimal; currency: str`
  - `async def quote_fee(session, *, portfolio_id: UUID, asset_class: AssetClass, side: str, notional: Decimal) -> FeeQuote`: `notional` va passato già nella valuta della regola. La conversione dalla valuta dello strumento la fa il motore (Piano 3), quindi per ora non serve conoscerla: chi chiama legge `currency` dal `FeeQuote`.

- [ ] **Step 1: Scrivi i test che falliscono**

```python
def test_portfolio_beats_asset_class_beats_global(): ...
def test_side_filter():                      # una regola solo "sell" non vale per un "buy" → si usa la globale
def test_inactive_ignored(): ...
def test_priority_breaks_tie(): ...
def test_none_found():                       # → FEE_RULE_NOT_FOUND
async def test_quote_fee_from_db(db_session, portfolio):   # globale di default + portafoglio 0,05% → usa quella del portafoglio
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa.** **Step 4: Esegui** → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: fee rule resolution and quoting"`

---

### Task 10: Seed idempotente e avvio completo in Docker

**Files:**
- Create: `backend/src/smtrading/seed.py`
- Modify: `backend/docker/entrypoint-api.sh` (abilita migrazioni e seed)
- Test: `backend/tests/integration/test_seed.py`

**Interfaces:**
- Consumes: `create_portfolio` (Task 7), modelli `Instrument` e `FeeRule`.
- Produces: `async def run_seed(session) -> None` (non fa commit) e `main()` per `python -m smtrading.seed` (apre la sessione, chiama `run_seed`, fa commit).
  - **Strumenti** (upsert per `symbol`):

    | Simbolo | Classe | Mercato | Valuta | Lotto | Tick |
    |---|---|---|---|---|---|
    | AAPL, MSFT, NVDA | us_equity | NASDAQ | USD | 0.000001 | 0.01 |
    | SPY | us_equity | NYSE | USD | 0.000001 | 0.01 |
    | ENI.MI, ISP.MI | eu_equity | MTA | EUR | 1 | 0.001 |
    | SAP.DE, VWCE.DE | eu_equity | XETRA | EUR | 1 | 0.01 |
    | BTC-USD, ETH-USD | crypto | CRYPTO | USD | 0.00000001 | 0.01 |
    | EURUSD, GBPUSD | forex | FX | USD | 1 | 0.00001 |

    Le azioni e gli ETF sono `shortable=True`.
  - **Regola di commissione** "Default globale", se non ne esiste una con quel nome: global, both, EUR, fissa 1, 0,1%, min 1, max 20, fx 0,25.
  - **Portafoglio "Demo"** con 100.000 EUR, solo se non esiste già un portafoglio con quel nome.

- [ ] **Step 1: Scrivi il test che fallisce**

```python
async def test_seed_idempotent(db_session):
    await run_seed(db_session); await run_seed(db_session)
    demo = (await db_session.execute(select(Portfolio).where(Portfolio.name == "Demo"))).scalars().all()
    assert len(demo) == 1
    assert await get_cash_balances(db_session, demo[0].id) == {"EUR": Decimal("100000")}
    assert (await get_instrument(db_session, "btc-usd")).asset_class == AssetClass.crypto
    rules = await load_rules(db_session)
    assert len([r for r in rules if r.scope == FeeScope.global_]) == 1
```

- [ ] **Step 2: Esegui** → FAIL. **Step 3: Implementa** `seed.py` e aggiorna `entrypoint-api.sh`. **Step 4: Esegui** → PASS.
- [ ] **Step 5: Esegui tutta la suite** `uv run pytest -v && uv run ruff check .` → tutto PASS.
- [ ] **Step 6: Verifica in Docker** `docker compose up -d --build` → `curl -s localhost:8000/health` → `"status":"ok"`; poi `docker compose restart api` e `docker compose exec postgres psql -U smtrading -c "select name from portfolios"` → una sola riga "Demo".
- [ ] **Step 7: Commit** `git commit -m "feat: idempotent seed and full docker startup"`
