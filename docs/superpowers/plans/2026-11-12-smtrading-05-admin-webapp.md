# SMTrading — Piano 5: API di amministrazione e web app (italiano)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un'API REST per l'admin (`/api/...`) con login, TOTP opzionale, CSRF e WebSocket per il tempo reale, e una web app React in italiano, con tema scuro e chiaro, servita da nginx su `localhost:3000`. Copre tutte le pagine della spec §10, tranne Broker (piano 6).

**Architecture:**
- **Backend:** router FastAPI in `smtrading/admin/routes_*.py`, sottili sopra i servizi di dominio dei piani 1-4. La sessione admin vive in Redis (`adm:sess:{id}`, TTL 12h) e il cookie `smt_session` è httpOnly, SameSite=Lax. Il CSRF usa il double-submit: cookie `smt_csrf` + header `X-CSRF-Token` su ogni metodo non GET. `/ws` inoltra gli eventi di Redis (`events`, `ticks:{sym}`).
- **Frontend** in `web/`: Vite + React 18 + TypeScript, Tailwind + shadcn/ui, TanStack Query, React Router, Lightweight Charts (equity e prezzi), Recharts (statistiche), dnd-kit (priorità).
- Il container `web` è una build multi-stage node → nginx, con proxy di `/api` e `/ws` verso `api:8000`.
- Questo piano parte dal branch del piano 4.

**Tech Stack:** FastAPI, pyotp, argon2-cffi, itsdangerous; Node 22 (build nel container), Vite, React, TS, Tailwind, shadcn/ui, TanStack Query, lightweight-charts, recharts, @dnd-kit, vitest, Playwright (immagine ufficiale `mcr.microsoft.com/playwright`).

**Spec:** `docs/superpowers/specs/2026-11-12-smtrading-design.md` (§10, §11 backup, §6 kill-switch, §7 commissioni)

## Global Constraints

- Valgono i vincoli dei piani 1-4. Gli handler fanno il commit della sessione; i servizi no.
- Tutta l'interfaccia è in **italiano**. Il nome visualizzato è **SMTrading**. I numeri seguono il formato `it-IT`: separatore decimale `,`, migliaia `.`; i valori in EUR si mostrano come `1.234,56 €`.
- **Tema:** scuro per impostazione predefinita, con interruttore chiaro/scuro salvato in `localStorage` (con try/catch).
  - Token CSS: `--bg`, `--fg`, `--muted`, `--card`, `--border`, `--accent` (emerald-500), `--gain` (emerald-500), `--loss` (rose-500), `--warn` (amber-500).
  - Font: Inter per il testo, JetBrains Mono per numeri e tabelle (tabular-nums).
- **Errori dell'API:** risposta JSON `{"code","message","details"}`. `SMTError` → 400, tranne `NOT_FOUND` → 404 e `FORBIDDEN` → 403. Non autenticato → 401 con `{"code":"FORBIDDEN","message":"login richiesto"}`. CSRF mancante o errato → 403 con `{"code":"FORBIDDEN","message":"CSRF"}`.
- Nelle risposte i Decimal sono stringhe; il frontend li tratta come stringhe e li formatta senza passare da float.
- Il token di un agente viene restituito **solo** dalle chiamate di creazione e rotazione.
- Le chiavi API dei connettori non vengono mai restituite per intero: si mostrano gli ultimi 4 caratteri (`••••abcd`).
- Porte: `web` 3000 sull'host; `api` 8000 resta esposto (serve all'MCP e al debug).
- Backup: servizio `backup` in compose che ogni 24h esegue `pg_dump | gzip` in `/backups/smtrading-YYYY-MM-DD.sql.gz` e conserva 14 giorni.

- Validazione delle regole di commissione: gli scaglioni sono ordinati per `up_to` crescente e l'ultimo ha `up_to=null`; i valori degli scaglioni sono salvati nel JSONB come **stringhe** (JSONB non serializza i Decimal).
- Nome di portafoglio duplicato → 400 con codice `INVALID_ORDER` e `details.field="name"`. In più va gestita l'`IntegrityError` di una creazione concorrente, trasformandola nello stesso errore.
- `/health` risponde 503 quando lo stato è degradato.

## Review Focus

1. **Sessione scaduta mentre la web app è aperta** → la prima chiamata riceve 401, il client porta al login conservando la pagina di ritorno e non resta in loop di errori. Test in Task 6.
2. **Doppio clic su "Deposita" o "Storna"** → una sola operazione: il pulsante è disabilitato mentre la richiesta è in corso e il backend usa un header `Idempotency-Key` per i POST che muovono soldi. Test in Task 2 (backend) e Task 8 (UI).
3. **Storno con motivazione vuota o di soli spazi** → 400 `INVALID_REVERSAL`, messaggio mostrato vicino al campo. Test in Task 2 e Task 8.
4. **Kill-switch attivato da una scheda mentre un'altra è aperta** → anche l'altra scheda lo vede entro pochi secondi, grazie all'evento `kill_switch` sul WebSocket. Test in Task 5 e Task 7.
5. **Tabella dei movimenti con migliaia di righe** → paginazione a cursore lato server (`before_seq`), con l'export CSV in streaming. Test in Task 2.

---

## File Structure

```
backend/src/smtrading/admin/
  __init__.py, models.py (AdminUser), auth.py (password, TOTP, sessioni, CSRF, dipendenza require_admin),
  idempotency.py, errors.py (exception handler), schemas.py (Pydantic request/response),
  routes_auth.py, routes_portfolios.py, routes_agents.py, routes_fees.py, routes_risk.py,
  routes_connectors.py, routes_markets.py, routes_system.py, routes_dashboard.py, ws.py
  portfolio_ops.py   # reset_portfolio
backend/migrations/versions/0007_admin.py
backend/tests/integration/test_admin_*.py
web/
  Dockerfile, nginx.conf, package.json, vite.config.ts, tsconfig.json, tailwind.config.ts, index.html
  src/main.tsx, App.tsx, routes.tsx, theme.ts, index.css
  src/api/client.ts (fetch + CSRF + 401), src/api/hooks/*.ts (TanStack Query), src/api/types.ts
  src/lib/format.ts (importi, percentuali, date in it-IT), src/lib/ws.ts (WebSocket con riconnessione)
  src/components/ui/* (shadcn), src/components/{Layout,Sidebar,KillSwitchButton,Money,Pnl,DataTable,ConfirmDialog,EquityChart}.tsx
  src/pages/{Login,Dashboard,Portfolios,PortfolioDetail,Agents,Fees,Risk,Connectors,Markets,McpLog,Settings}.tsx
  src/**/*.test.ts(x) (vitest)
  e2e/*.spec.ts (Playwright)
docker-compose.yml (servizi web, backup)
```

---

### Task 1: Autenticazione admin, sessioni, CSRF, TOTP

**Files:** `admin/{models,auth,errors,routes_auth}.py`, `migrations/versions/0007_admin.py`, `seed.py` (admin da `SMT_ADMIN_EMAIL` e `SMT_ADMIN_PASSWORD`, creato solo se manca), `api/main.py` (exception handler, router). Test: `tests/integration/test_admin_auth.py`.

**Interfaces:**
- `AdminUser`: `id, email UNIQUE, password_hash, totp_secret_encrypted bytea|None, totp_enabled bool, created_at, last_login_at`.
- `async def login(session, redis, email, password, totp: str|None) -> tuple[str, str]` → (session_id, csrf):
  - credenziali errate → `FORBIDDEN` "credenziali non valide", con lo stesso messaggio per email e password;
  - con TOTP attivo e codice mancante → `FORBIDDEN` con `details={"totp_required": true}`; codice errato → `FORBIDDEN`;
  - rate limit: 5 tentativi falliti per email in 15 minuti → `RATE_LIMITED`.
- `require_admin` (dipendenza FastAPI) → `AdminUser`. Rinnova il TTL della sessione; per i metodi non GET verifica il CSRF.
- Route:
  - `POST /api/auth/login {email,password,totp?}` → imposta i cookie `smt_session` (httpOnly) e `smt_csrf` (leggibile da JS), restituisce `{email, totp_enabled}`;
  - `POST /api/auth/logout`, `GET /api/auth/me`;
  - `POST /api/auth/totp/setup` → `{secret, otpauth_uri}` (non ancora attivo);
  - `POST /api/auth/totp/enable {code}`, `POST /api/auth/totp/disable {code}`;
  - `POST /api/auth/password {current, new}` (minimo 10 caratteri).
- Handler di `SMTError` secondo i Global Constraints (vale anche per tutte le route successive).
- Test:
  - login ok, poi `me`;
  - password errata, poi rate limit;
  - flusso TOTP (pyotp genera il codice);
  - CSRF mancante su un POST → 403;
  - logout invalida la sessione;
  - seed admin idempotente;
  - nessuna route `/api/*` tranne `login` è accessibile senza cookie (test parametrizzato su `app.routes`).
- [ ] FAIL → implementa → PASS → commit `feat(admin): auth, sessions, csrf, totp`

---

### Task 2: API di portafogli, movimenti, ordini e posizioni

**Files:** `admin/{schemas,idempotency,routes_portfolios,portfolio_ops}.py`. Test: `tests/integration/test_admin_portfolios.py`.

**Interfaces:**
- `idempotency`: dipendenza `idempotent(scope)`. Se c'è `Idempotency-Key`, salva in Redis `idem:{scope}:{key}` (TTL 24h) la risposta JSON della prima esecuzione e la restituisce identica per le ripetizioni; se la stessa chiave arriva mentre la prima richiesta è ancora in corso → 409.
- Route, tutte sotto `require_admin`:
  - `GET /api/portfolios` → lista con equity in EUR e P&L del giorno; `POST /api/portfolios {name, initial_cash, base_currency="EUR", leverage=1, slippage_bps=0, timezone, risk_profile_id?}`;
  - `GET /api/portfolios/{id}`; `PATCH /api/portfolios/{id}` con `name, leverage, maintenance_margin_pct, warning_margin_pct, slippage_bps, risk_profile_id, timezone, status`:
    - `status` accetta `active` (che sblocca anche `reduce_only`) o `locked`;
    - leva ammessa tra 1 e 10;
  - `GET /{id}/valuation`, `GET /{id}/positions`, `GET /{id}/performance?period=`;
  - `GET /{id}/orders?status=&limit=`, `POST /api/orders/{order_id}/cancel`;
  - `GET /{id}/transactions?type=&from=&to=&before_seq=&limit=50` → `{items, next_before_seq}`;
  - `GET /{id}/transactions.csv` (StreamingResponse; colonne: seq, data, tipo, asset, conto, quantità, prezzo, valuta, motivazione, id_ordine, id_journal; separatore `;`, decimali con la virgola);
  - `POST /{id}/deposit {amount, currency, reason?}`, `POST /{id}/withdraw {...}` (idempotenti);
  - `POST /api/journals/{journal_id}/reverse {reason}` (idempotente). Usa `reverse_journal` del piano 1 con `allow_negative` derivato dal tipo del journal originale: per TRADE, LIQUIDATION e CORPORATE_ACTION `{position}`, altrimenti vuoto. Questo chiude il punto parcheggiato P1 del piano 1; se serve, estendi `reverse_journal` con il parametro `allow_negative`;
  - `POST /{id}/reset {new_cash, currency="EUR", reason}` → `portfolio_ops.reset_portfolio`:
    1. annulla gli ordini aperti;
    2. se ci sono posizioni e uno dei loro mercati è chiuso → `INVALID_ORDER` "posizioni aperte su mercati chiusi";
    3. chiude le posizioni a mercato (`source="admin"`);
    4. azzera ogni saldo cash, collateral e margin_loan con un journal `RESET` contro `system_equity`;
    5. deposita `new_cash`.
- Test:
  - CRUD di un portafoglio;
  - deposito e poi saldo;
  - la stessa `Idempotency-Key` due volte → un solo journal;
  - storno con motivazione vuota → 400 `INVALID_REVERSAL`;
  - storno di un TRADE short (P1) → ok;
  - paginazione: 120 depositi → 3 pagine coerenti;
  - CSV con l'intestazione italiana e la virgola decimale;
  - reset con posizioni a mercato aperto (FakeQuoteSource) → cash = new_cash e nessuna posizione;
  - PATCH della leva oltre 10 → 400.
- [ ] FAIL → implementa → PASS → commit `feat(admin): portfolio, ledger, order endpoints`

---

### Task 3: API di agenti, audit, commissioni, rischio, impostazioni, kill-switch

**Files:** `admin/{routes_agents,routes_fees,routes_risk}.py`. Test: `tests/integration/test_admin_config.py`.

**Interfaces:**
- **Agenti:**
  - `GET /api/agents` (con i portafogli assegnati e `last_seen_at`); `POST /api/agents {name}` → `{agent, token}`;
  - `POST /api/agents/{id}/rotate` → `{token}`; `PATCH /api/agents/{id} {active}`;
  - `PUT /api/agents/{id}/portfolios [{portfolio_id, permission}]`, che sostituisce l'intero insieme;
  - `GET /api/mcp-audit?agent_id=&method=&outcome=&since=&limit=`.
- **Commissioni:**
  - `GET/POST /api/fee-rules`, `PATCH/DELETE /api/fee-rules/{id}`. DELETE vuol dire `active=false`: le regole non vengono mai cancellate, perché i journal fanno riferimento al `fee_rule_id`;
  - validazione: scaglioni ordinati con l'ultimo `up_to=null`, `min ≤ max`, percentuali ≥ 0;
  - `POST /api/fee-rules/simulate {portfolio_id, symbol, side, qty?, notional?}` → `{rule_id, rule_name, notional_eur, fee, fee_currency, fx_fee_eur}`, calcolato con `preview_order` del piano 3.
- **Rischio:**
  - `GET/POST /api/risk-profiles`, `PATCH /api/risk-profiles/{id}`;
  - `GET/PUT /api/interest-rates`;
  - `GET/PUT /api/settings/app` (`stale_quote_s`, `default_spread_bps`, `timezone`);
  - `GET /api/kill-switch`, `POST /api/kill-switch {on, cancel_open}`: pubblica l'evento `kill_switch` e restituisce `{on, canceled}`.
- Test:
  - il token compare solo in create e rotate (verificato che `GET` non lo contenga);
  - l'assegnazione sostituisce l'insieme;
  - filtri dell'audit;
  - scaglioni non validi → 400;
  - il simulatore usa la regola del portafoglio;
  - kill-switch con l'evento ricevuto da un subscriber Redis.
- [ ] FAIL → implementa → PASS → commit `feat(admin): agents, fees, risk, settings, kill switch endpoints`

---

### Task 4: API di connettori, mercati, sistema e backup

**Files:** `admin/{routes_connectors,routes_markets,routes_system}.py`; `docker-compose.yml` (servizio `backup`); `backend/Dockerfile` (`postgresql-client-16` per `pg_dump`). Test: `tests/integration/test_admin_connectors.py`.

**Interfaces:**
- **Connettori:**
  - `GET /api/connectors` → `[{name, enabled, requires_key, capabilities, asset_classes, implemented: bool (presente in PROVIDERS), config_masked, rate_limit_per_min, health_status, last_error, last_checked_at}]`;
  - `PATCH /api/connectors/{name} {enabled?, config?, rate_limit_per_min?}`: un valore di config uguale a `"••••xxxx"` lascia invariato il valore esistente;
  - `POST /api/connectors/{name}/test` → esegue `health_check` e `record_health`, restituisce `{ok, error}`;
  - `GET /api/provider-priorities` → matrice; `PUT /api/provider-priorities {asset_class, capability, providers}`.
- **Mercati:**
  - `GET /api/marketdata/stream-status` (legge `md:stream_status`);
  - `POST /api/marketdata/track {symbols}`, `POST /api/marketdata/untrack {symbols}` (fonte `admin`);
  - `GET /api/instruments?query=&asset_class=`, `POST /api/instruments` (nuovo strumento con validazione), `PATCH /api/instruments/{symbol}`;
  - `GET /api/quotes?symbols=`, `GET /api/markets/status`.
- **Sistema:**
  - `GET /api/system/verify-chain`, `GET /api/system/verify-balances`;
  - `GET /api/system/backup` → streaming di `pg_dump --format=custom` come download `smtrading-YYYYMMDD-HHMM.dump`;
  - `GET /api/system/backups` → elenco dei file nel volume `backups`, che è montato in sola lettura anche su `api`.
- Compose `backup`: immagine `postgres:16`, comando `sh -c 'while true; do pg_dump -h postgres -U smtrading smtrading | gzip > /backups/smtrading-$(date +%F).sql.gz; find /backups -name "*.sql.gz" -mtime +14 -delete; sleep 86400; done'`, con `PGPASSWORD`.
- Test:
  - la configurazione è mascherata;
  - un PATCH con il valore mascherato non sovrascrive;
  - priorità con un nome sconosciuto → 404;
  - `track` aggiorna `md:tracked:admin`;
  - verify-chain restituisce ok;
  - l'header di download del backup è presente: il test usa un `pg_dump` finto se non è disponibile localmente (monkeypatch del comando).
- [ ] FAIL → implementa → PASS → commit `feat(admin): connectors, markets, system, backup endpoints`

---

### Task 5: WebSocket ed endpoint della dashboard

**Files:** `admin/{ws,routes_dashboard}.py`. Test: `tests/integration/test_admin_ws.py`.

**Interfaces:**
- `GET /api/dashboard` → `{total_equity_eur, day_pnl_eur, day_pnl_pct, portfolios: [{id, name, equity_eur, day_pnl_pct, status}], connectors: [{name, health_status}], margin_alerts: [notifications recenti di tipo margin_warning o liquidation, al massimo 10], kill_switch, worker_heartbeat_age_s}`.
- `GET /api/dashboard/equity?period=1d|1w|1m|3m|1y|all` → serie aggregata di tutti i portafogli (somma degli snapshot per timestamp arrotondato a 5 minuti) più la serie per portafoglio.
- `WS /ws`:
  - autenticazione con il cookie di sessione; senza sessione la connessione viene chiusa con il codice 4401;
  - messaggi del client: `{"type":"subscribe_quotes","symbols":[...]}` / `{"type":"unsubscribe_quotes",...}`;
  - messaggi del server: `{"type":"event","event":{...}}` per ogni messaggio su `events`, `{"type":"quote","quote":{...}}` per `ticks:{sym}` sottoscritti, `{"type":"ping"}` ogni 25s.
- Test:
  - connessione WS senza cookie → chiusa con 4401;
  - con cookie: un evento pubblicato su Redis arriva sul WS; `subscribe_quotes` più `write_quote` → arriva la quota;
  - valori della dashboard con 2 portafogli.
- [ ] FAIL → implementa → PASS → commit `feat(admin): websocket relay and dashboard endpoints`

---

### Task 6: Scheletro della web app, client, login, layout, tema, Docker

**Files:** tutto `web/` di base; `docker-compose.yml` (servizio `web`). Test: `web/src/lib/format.test.ts`, `web/src/api/client.test.ts`.

**Interfaces:**
- `api/client.ts`: `apiFetch<T>(path, {method, body, idempotencyKey?}) → Promise<T>`.
  - Aggiunge `X-CSRF-Token` dal cookie `smt_csrf` per i metodi non GET, usa `credentials: "include"` e `Idempotency-Key` se fornita.
  - In caso di errore solleva `ApiError {status, code, message, details}`.
  - Con 401 porta a `/login?next=<path>`, una sola volta: un flag globale evita i loop.
- `lib/format.ts`:
  - `formatMoney(value: string, currency: string) → "1.234,56 €"`;
  - `formatQty(value: string)`, che conserva fino a 8 decimali significativi e toglie gli zeri finali;
  - `formatPct(value: string) → "+1,25%"`;
  - `formatDateTime(iso)` in `Europe/Rome`, `dd/MM/yyyy HH:mm:ss`.
  - Tutte lavorano su stringhe, con aritmetica su stringhe o BigInt dove serve; non c'è conversione a float per i valori mostrati.
- `lib/ws.ts`: `useLiveSocket()`, un hook con riconnessione esponenziale (1s → 30s) e un emitter tipizzato per `event` e `quote`.
- Pagine e layout:
  - `Login`: email, password e il campo TOTP, che compare quando la risposta contiene `totp_required`; i messaggi sono in italiano;
  - `Layout`: sidebar con le voci Dashboard, Portafogli, Agenti, Commissioni, Rischio e margine, Connettori, Mercati, Log MCP, Impostazioni; header con il nome SMTrading, l'indicatore del kill-switch, l'interruttore del tema e il logout;
  - tema secondo i Global Constraints.
- `web/Dockerfile`: stage `node:22-alpine` (`npm ci && npm run build`) → `nginx:1.27-alpine` con `nginx.conf`, che serve la SPA con `try_files ... /index.html` e fa il proxy di `/api/` e `/ws` (upgrade) verso `http://api:8000`.
- Compose: `web` dipende da `api` ed è esposto su `3000:80`.
- Test (vitest):
  - `formatMoney("1234.5","EUR") === "1.234,50 €"`, `formatMoney("-0.004","EUR") === "-0,00 €"` (oppure `"0,00 €"`: decidi e fissalo nel test), `formatPct("1.25") === "+1,25%"`, `formatQty("0.12345678000") === "0,12345678"`;
  - il client aggiunge il CSRF solo ai metodi non GET;
  - un 401 porta a `/login` una volta sola.
- Verifica: `docker compose up -d --build web` → `curl -s localhost:3000 | grep -i smtrading`; `curl -s localhost:3000/api/auth/me` → 401 JSON.
- [ ] FAIL → implementa → PASS → verifica → commit `feat(web): app shell, api client, login, theme, docker`

---

### Task 7: Pagina Dashboard

**Files:** `pages/Dashboard.tsx`, `components/{EquityChart,KillSwitchButton,Money,Pnl}.tsx`, `api/hooks/dashboard.ts`. Test: `pages/Dashboard.test.tsx` (con `msw` per mockare l'API).

**Contenuto:**
- card KPI: equity totale, P&L del giorno in € e %, numero di portafogli, heartbeat del worker (verde < 30s);
- `EquityChart` con Lightweight Charts, aggregato con selettore di periodo;
- tabella dei portafogli, con link al dettaglio;
- feed in tempo reale di ordini ed eseguiti dal WS (ultimi 50);
- stato dei connettori (pallino colorato);
- alert di margine;
- **KillSwitchButton**: pulsante rosso con dialog di conferma e checkbox "Annulla anche gli ordini aperti"; si aggiorna con l'evento `kill_switch`.
- Test: render con dati finti (P&L negativo con classe `loss`); l'evento WS `kill_switch` aggiorna il badge; conferma del kill-switch → POST con `cancel_open`.
- [ ] FAIL → implementa → PASS → commit `feat(web): dashboard`

---

### Task 8: Pagine Portafogli e dettaglio

**Files:** `pages/{Portfolios,PortfolioDetail}.tsx`, `components/{DataTable,ConfirmDialog}.tsx`, `api/hooks/portfolios.ts`. Test: `pages/PortfolioDetail.test.tsx`.

**Contenuto:**
- **Elenco:** tabella con nome, equity, P&L del giorno, stato, leva; pulsante "Nuovo portafoglio" (dialog: nome, saldo iniziale, valuta base, leva, slippage, profilo di rischio, fuso).
- **Dettaglio**, con le tab:
  - **Panoramica:** grafico dell'equity, KPI, cash per valuta.
  - **Posizioni:** prezzi live via WS, P&L non realizzato colorato.
  - **Ordini:** filtri di stato, pulsante "Annulla".
  - **Movimenti:** paginazione a cursore, filtri per tipo e date, "Esporta CSV", azione "Storna" con dialog e motivazione obbligatoria.
  - **Operazioni:** deposito, prelievo e reset, con conferma e `Idempotency-Key` generata per ogni apertura del dialog.
  - **Rischio:** profilo, leva, soglie, distanza dal mantenimento e un pulsante "Sblocca" se il portafoglio è in `reduce_only` o `locked`.
- Test:
  - il pulsante "Deposita" è disabilitato durante la richiesta (Review Focus 2);
  - la motivazione dello storno è obbligatoria, con il messaggio vicino al campo;
  - la paginazione carica la pagina successiva;
  - il CSV è un link che punta a `/api/portfolios/{id}/transactions.csv`.
- [ ] FAIL → implementa → PASS → commit `feat(web): portfolios pages`

---

### Task 9: Pagine Agenti e Log MCP

**Files:** `pages/{Agents,McpLog}.tsx`, `api/hooks/agents.ts`. Test: `pages/Agents.test.tsx`.

**Contenuto:**
- **Agenti:**
  - tabella con nome, stato, ultimo accesso e portafogli;
  - "Nuovo agente": un dialog mostra il token **una sola volta**, con il pulsante "Copia" e l'avviso "Non sarà più visibile";
  - "Rigenera token", con conferma e poi lo stesso dialog;
  - "Disattiva" e "Attiva";
  - editor dei portafogli assegnati (multi-select con permesso `read`/`trade`).
- **Log MCP:** tabella filtrabile (agente, metodo, esito, data), con i parametri JSON espandibili e la latenza.
- Test: alla chiusura del dialog il token non è più nel DOM; il PUT dei portafogli contiene l'insieme completo.
- [ ] FAIL → implementa → PASS → commit `feat(web): agents and MCP log pages`

---

### Task 10: Pagine Commissioni e Rischio e margine

**Files:** `pages/{Fees,Risk}.tsx`, `api/hooks/{fees,risk}.ts`. Test: `pages/Fees.test.tsx`.

**Contenuto:**
- **Commissioni:**
  - tabella delle regole (portata, riferimento, lato, fissa, %, min, max, scaglioni, cambio, priorità, attiva);
  - editor in un dialog, con gli scaglioni dinamici (aggiungi e rimuovi righe, l'ultimo "oltre");
  - "Disattiva";
  - **Simulatore:** portafoglio, simbolo, lato, quantità o controvalore → mostra la regola applicata, la commissione e il cambio.
- **Rischio e margine:**
  - profili di rischio (CRUD; whitelist come tag);
  - tabella dei tassi di interesse modificabile;
  - impostazioni delle quote vecchie e degli spread di default per classe di asset;
  - kill-switch (lo stesso componente della Dashboard).
- Test: validazione degli scaglioni lato client (l'ultimo deve essere "oltre"); il simulatore mostra il risultato restituito dall'API.
- [ ] FAIL → implementa → PASS → commit `feat(web): fees and risk pages`

---

### Task 11: Pagine Connettori, Mercati e Impostazioni

**Files:** `pages/{Connectors,Markets,Settings}.tsx`, `api/hooks/{connectors,markets,system}.ts`. Test: `pages/Connectors.test.tsx`.

**Contenuto:**
- **Connettori:**
  - una card per connettore con attivazione, campi chiave (mascherati e modificabili), rate limit, "Testa connessione" con l'esito, stato e ultimo errore;
  - i connettori non ancora implementati (`implemented=false`) mostrano il badge "Disponibile prossimamente" e l'interruttore disattivato;
  - matrice delle priorità: per classe di asset × capacità, una lista ordinabile con drag & drop (dnd-kit) e salvataggio;
  - stato dello streaming: simboli seguiti, stato (connesso o in attesa di riconnessione) e simboli in polling.
- **Mercati:**
  - ricerca degli strumenti, con l'aggiunta di uno strumento all'anagrafica dai risultati;
  - griglia delle quote in tempo reale (sottoscrizione WS) per i simboli seguiti;
  - pulsanti "Segui" e "Smetti di seguire";
  - tabella degli orari dei mercati (aperto o chiuso, prossima apertura o chiusura).
- **Impostazioni:** fuso orario; "Verifica integrità dello storico" (chain + saldi, con l'esito); "Scarica backup ora"; elenco dei backup automatici.
- Test: drag & drop che riordina e invia il PUT, testato con lo stato del componente e una funzione di riordino pura (`reorder(list, from, to)`); il campo chiave mascherato non viene inviato se non è stato modificato.
- [ ] FAIL → implementa → PASS → commit `feat(web): connectors, markets, settings pages`

---

### Task 12: E2E con Playwright e verifica finale in Docker

**Files:** `web/e2e/{login,portfolio,fees,killswitch}.spec.ts`, `web/playwright.config.ts`, `scripts/e2e.sh`.

- `scripts/e2e.sh`:
  1. `docker compose up -d --build`;
  2. aspetta `/health` ok;
  3. `docker run --rm --network host -v $PWD/web:/work -w /work mcr.microsoft.com/playwright:v1.49.0-jammy npx playwright test`, con `BASE_URL=http://localhost:3000` e le credenziali admin prese da `.env`.
- Scenari:
  - login (e logout);
  - creazione del portafoglio "E2E-<timestamp>" da 5000 €, poi deposito di 100 € → il saldo mostra 5.100,00 €, poi lo storno del deposito con motivazione → il saldo torna a 5.000,00 €;
  - creazione di una regola di commissione per quel portafoglio e simulazione;
  - kill-switch attivato e disattivato, con il badge nell'header.
- [ ] Scrivi gli spec → `bash scripts/e2e.sh` → tutto verde → commit `test(web): playwright e2e`
