# SMTrading — Design Spec

**Data:** 2026-11-12
**Stato:** in revisione

## 1. Obiettivo

SMTrading è una piattaforma di **trading simulato su dati di mercato reali**. Gli agenti AI (Paperclip, già in esecuzione in un container Docker) operano tramite un **server MCP** su portafogli virtuali precaricati di denaro. Un amministratore configura tutto da una **web app in italiano**: portafogli, agenti, commissioni, rischio, connettori dati, broker paper.

Ogni movimento di denaro o titoli è registrato in modo **immutabile** nello storico del portafoglio.

### Criteri di successo
- Paperclip si collega all'MCP, legge i metodi, consulta i dati e piazza ordini buy/sell su più portafogli.
- Ogni ordine eseguito produce movimenti contabili coerenti (partita doppia, somma zero), con commissioni calcolate secondo le regole impostate dall'admin.
- Le quote arrivano in tempo reale (streaming) per i simboli seguiti, e fin dal primo avvio funzionano senza nessuna chiave API grazie ai fornitori gratuiti.
- Tutto gira in locale su Docker Desktop (Windows/WSL2) con un solo `docker compose up`.

### Fuori scope (per ora)
- Trading con denaro reale (Revolut X solo come fonte dati; Trading 212 e Alpaca solo in modalità paper/demo).
- Più utenti admin o ruoli.
- Esposizione su LAN o Internet.
- Strategie di backtest scritte dall'agente come codice o regole libere.
- Esecuzioni parziali simulate.

## 2. Decisioni prese

| Tema | Decisione |
|---|---|
| Esecuzione | Motore simulato interno; replica opzionale per portafoglio su Alpaca Paper / Trading 212 Demo. **La fonte di verità è il nostro registro** |
| Strumenti | Azioni/ETF USA ed europei, crypto, forex |
| Ordini | Market, Limit, Stop, Stop-limit, Trailing stop, OCO, Bracket; durata DAY/GTC |
| Esecuzione simulata | Al bid/ask + slippage configurabile; ordini **sempre eseguiti per intero** |
| Fuori orario | Ordine in coda, eseguito all'apertura |
| Short e leva | Sì; leva per portafoglio, margin call con liquidazione (prima le posizioni più in perdita), interessi giornalieri |
| Portafogli | Ogni agente può averne N; multivaluta con base EUR; commissione sul cambio |
| Commissioni | Fissa + % con min/max, scaglioni, cambio; regole globale → tipo di strumento → portafoglio (vince la più specifica) |
| Rischio | Limiti per ordine e posizione, perdita max giornaliera (→ solo riduzione), whitelist simboli, kill-switch globale |
| Dati | Yahoo, Alpaca, Finnhub, Twelve Data, Binance, Kraken, CoinGecko, Revolut X (solo dati), BCE/Frankfurter, indicatori interni |
| Real-time | Streaming per i simboli seguiti, polling con cache per gli altri |
| Storico | Partita doppia in sola aggiunta, trigger contro modifiche e cancellazioni, catena di hash; correzioni solo tramite storno con motivazione |
| MCP | Streamable HTTP, bearer token per agente; gli agenti non gestiscono fondi né portafogli |
| Backtest | Strategie predefinite parametrizzabili |
| Admin | Un solo admin, TOTP opzionale, UI italiana, tema scuro e chiaro |
| Stack | Python 3.12 + FastAPI + SDK MCP ufficiale, PostgreSQL 16, Redis 7, React + Vite + TS |
| Rete | Solo localhost; Paperclip collegato alla rete Docker `smtrading-net` |

## 3. Architettura

Un solo backend Python diviso in moduli, che gira come due processi, più i servizi di contorno.

| Servizio | Ruolo | Porta host |
|---|---|---|
| `postgres` | Database principale (registro, ordini, configurazione) | — |
| `redis` | Cache delle quote, canale eventi in tempo reale, lock distribuiti | — |
| `api` | API REST admin, WebSocket dashboard, MCP su `/mcp` | 8000 |
| `worker` | Streaming prezzi, controllo ordini in attesa, margine, interessi, dividendi e split, alert, snapshot equity, backtest, riconciliazione broker | — |
| `web` | Web app React servita da nginx (proxy verso `/api` e `/ws`) | 3000 |

Paperclip (container esistente) → `docker network connect smtrading-net <container>` → `http://api:8000/mcp`.

### Moduli del pacchetto `smtrading`
- `ledger`: registro a partita doppia, saldi, storni, catena di hash.
- `instruments`: anagrafica dei simboli, orari dei mercati, calendari delle festività.
- `marketdata`: interfaccia `DataProvider`, router con priorità e fallback, cache, rate limit, gestione dello streaming.
- `engine`: ciclo di vita degli ordini, calcolo dei prezzi di esecuzione, OCO, bracket, trailing.
- `fees`: risoluzione delle regole e calcolo delle commissioni.
- `risk`: controlli prima dell'ordine, margine, liquidazione, perdita giornaliera, kill-switch.
- `brokers`: interfaccia `BrokerAdapter`, replica degli ordini, riconciliazione.
- `corporate`: dividendi, split, interessi.
- `backtest`: motore di backtest con strategie predefinite.
- `mcp`: definizione di metodi e risorse MCP, autenticazione, audit.
- `admin`: API REST, autenticazione admin, WebSocket.
- `common`: configurazione, cifratura, clock, errori.

Regola: `mcp` e `admin` sono strati sottili sopra gli stessi servizi di dominio. Nessuna logica di business vive nei livelli di trasporto.

## 4. Modello dati

- `agents`: id, nome, hash del token (argon2), prefisso del token, attivo, creato il.
- `agent_portfolios`: agent_id, portfolio_id, permesso (`read` | `trade`).
- `portfolios`: id, nome, valuta base (EUR), leva, margine di mantenimento %, warning %, risk_profile_id, broker_link_id (nullable), slippage in bps, stato (`active` | `reduce_only` | `locked`), fuso orario.
- `instruments`: symbol (canonico, es. `AAPL`, `ENI.MI`, `BTC-USD`, `EURUSD`), classe di asset, mercato, valuta, lotto, tick, shortable, mappature dei simboli per fornitore.
- `accounts`: id, portfolio_id (nullable per i conti di sistema), asset (valuta o simbolo), tipo (`cash` | `position` | `collateral` | `system_fees` | `system_fx` | `system_interest` | `system_equity` | ...).
- `journals`: id, tipo (`DEPOSIT`, `WITHDRAWAL`, `TRADE`, `FEE`, `FX`, `INTEREST`, `DIVIDEND`, `CORPORATE_ACTION`, `LIQUIDATION`, `RESET`, `REVERSAL`), riferimenti (order_id, fill_id, reversed_journal_id), autore (agente o admin), motivazione, created_at, `prev_hash`, `hash`.
- `journal_lines`: journal_id, account_id, quantità (NUMERIC(38,18)), prezzo unitario e valuta per informazione.
- `account_balances`: saldo materializzato, aggiornato nella stessa transazione del journal.
- `orders`: id, portfolio_id, agent_id, client_order_id (univoco per portafoglio), simbolo, lato, quantità o importo, tipo, limit, stop, parametri trailing, durata, stato, parent_id e oco_group_id, broker_order_id, timestamp, motivo del rifiuto.
- `fills`: order_id, quantità, prezzo, fonte (`sim` | `broker`), quota usata (bid, ask, timestamp), commissione, journal_id.
- `positions` (vista o materializzata): portafoglio, simbolo, quantità con segno, prezzo medio, P&L realizzato.
- `fee_rules`: portata (`global` | `asset_class` | `portfolio`), riferimento alla portata, lato (buy/sell/both), fissa, %, minimo, massimo, scaglioni (JSON), % sul cambio, priorità, attiva.
- `risk_profiles`: importo massimo per ordine, peso massimo di una posizione in %, perdita giornaliera massima in % o in valore assoluto, whitelist (simboli, classi di asset, mercati), short consentito.
- `interest_rates`: valuta o classe di asset, tasso annuo sul debito, tasso annuo di prestito titoli per gli short.
- `connectors`: tipo, attivo, configurazione cifrata (AES-GCM, chiave master da `.env`), rate limit, stato di salute, ultimo errore.
- `provider_priorities`: classe di asset, funzione, lista ordinata di connettori.
- `broker_links`: tipo (`alpaca_paper` | `t212_demo`), credenziali cifrate, stato.
- `watchlists`, `watchlist_items`, `alerts` (simbolo, condizione, valore, stato, scattato il).
- `equity_snapshots`: portafoglio, timestamp, equity in EUR, cash, margine usato.
- `backtests`: parametri, stato, risultati (JSON).
- `mcp_audit_log`: agent_id, metodo, parametri (JSON, con i segreti oscurati), esito o codice di errore, latenza, timestamp.
- `admin_users`, `settings`, `system_state` (kill-switch).

### Invarianti del registro
1. Per ogni journal e ogni asset, la somma delle righe è 0.
2. Su `journals` e `journal_lines` modifiche e cancellazioni sono vietate da trigger.
3. `hash = sha256(prev_hash || contenuto canonico del journal)`; c'è un endpoint di verifica della catena.
4. Il saldo materializzato è sempre uguale alla somma delle righe (verificato nei test e da un job notturno).
5. Il cash non può andare sotto zero, salvo dove il margine lo consente: il debito a leva è registrato su un conto `margin_loan` dedicato.

### Esempio di journal TRADE
Acquisto di 10 AAPL a $190,00 (ask + slippage) in un portafoglio con solo EUR:
1. Journal `FX`: −€X dal cash EUR, +$1.901 sul cash USD, commissione sul cambio verso `system_fx`.
2. Journal `TRADE`: +10 sul conto AAPL, −$1.900 dal cash USD, con controparte `system_equity`; −$1 di commissione dal cash USD, +$1 su `system_fees`.

Tutto avviene in un'unica transazione insieme a `fill` e `order`.

## 5. Motore degli ordini

**Stati:** `NEW → ACCEPTED → (QUEUED | PENDING | TRIGGERED) → FILLED`; oppure `REJECTED`, `CANCELED`, `EXPIRED`.

1. **Validazione:** simbolo esistente e in whitelist, lotto e tick rispettati, permesso `trade` dell'agente, kill-switch spento, portafoglio non `locked`, `reduce_only` rispettato.
2. **Controllo rischio prima dell'ordine:** importo massimo per ordine, peso massimo della posizione, perdita giornaliera, buying power (= equity × leva − margine usato − commissioni e cambio stimati), short consentito.
3. **Mercato:** se è aperto, un market viene eseguito subito; se è chiuso va `QUEUED` ed è eseguito all'apertura (con la prima quota valida). Le crypto sono sempre aperte.
4. **Quota vecchia:** se è più vecchia della soglia (default 60s, configurabile per classe di asset) l'ordine market viene rifiutato con `STALE_QUOTE`.
5. **Prezzo di esecuzione:** buy = ask × (1 + slippage); sell = bid × (1 − slippage). Se manca il bid/ask si usa l'ultimo prezzo ± metà dello spread di default della classe di asset.
6. **Ordini in attesa:** il worker valuta a ogni tick solo gli ordini aperti su quel simbolo.
   - Limit buy quando ask ≤ limite; limit sell quando bid ≥ limite. Prezzo di esecuzione = il migliore tra limite e quota.
   - Stop: al tocco diventa market. Stop-limit: al tocco diventa limit.
   - Trailing: aggiorna il massimo raggiunto (o il minimo per gli short); distanza in % o in valore assoluto.
   - OCO: l'esecuzione di uno annulla l'altro nella stessa transazione.
   - Bracket: TP e SL vengono creati in OCO all'esecuzione dell'ordine padre.
7. **Concorrenza:** lock per portafoglio (`SELECT ... FOR UPDATE` sulla riga del portafoglio) per ogni esecuzione.
8. **DAY:** scade a fine seduta del mercato dello strumento.
9. **Broker paper collegato:** dopo l'accettazione l'ordine viene replicato. L'eseguito del broker sostituisce quello simulato (prezzo e quantità del broker, eventuali esecuzioni parziali del broker comprese), con le commissioni nostre. I tipi di ordine non supportati dal broker restano solo nel nostro motore e la dashboard lo segnala.

## 6. Rischio, margine ed eventi societari

- **Margine iniziale** = 1/leva; **mantenimento** = % del margine iniziale (default 50%); **warning** = soglia intermedia.
- **Controllo:** a ogni tick per i portafogli con leva o short, e comunque ogni 60s.
  - Sotto la soglia di warning: alert all'agente (`get_triggered_alerts`) e alla dashboard.
  - Sotto il mantenimento: liquidazione automatica con ordini market, prima le posizioni più in perdita, finché il portafoglio torna in regola. Journal `LIQUIDATION`.
- **Short:** il ricavato va su un conto `collateral`.
- **Interessi:** job giornaliero alle 00:00 UTC, idempotente per data; tasso annuo / 360 sul debito a leva e sul controvalore degli short. Journal `INTEREST`.
- **Perdita giornaliera:** equity confrontata con lo snapshot di inizio giornata nel fuso del portafoglio. Se viene superata il portafoglio passa a `reduce_only` fino al giorno dopo o allo sblocco dell'admin.
- **Kill-switch globale:** rifiuta ogni nuovo ordine con `KILL_SWITCH_ACTIVE` e, a scelta, annulla gli ordini aperti.
- **Dividendi e split:** job giornaliero dal calendario dei fornitori. Dividendo: accredito sulle posizioni long, addebito sulle short. Split: ricalcolo di quantità e prezzo medio. Journal `DIVIDEND` / `CORPORATE_ACTION`.

## 7. Commissioni

- **Risoluzione:** si raccolgono le regole attive che corrispondono (portafoglio → classe di asset → globale, filtrate per lato) e vince la più specifica; a parità di specificità vince la priorità più alta.
- **Calcolo:** `fee = clamp(fissa + controvalore × %, min, max)`. Con gli scaglioni, la % è quella dello scaglione del controvalore.
- **Cambio:** `fx_fee = importo convertito × % cambio`, registrata nel journal FX.
- Le commissioni sono espresse nella valuta della regola e convertite nella valuta del regolamento al cambio corrente.
- Ogni journal FEE riporta il riferimento alla regola applicata.

## 8. Connettori

### `DataProvider`
Capacità: `quote`, `bars`, `stream`, `news`, `fundamentals`, `calendar`, `fx`, `search`, `screener`. Ogni connettore dichiara classi di asset, capacità e rate limit.

| Connettore | Asset | Capacità | Chiave | Attivo al primo avvio |
|---|---|---|---|---|
| Yahoo Finance | USA, UE, forex, crypto | quote, bars, fundamentals, calendar, search, screener | no | sì |
| Binance (pubblico) | crypto | quote, bars, stream | no | sì |
| Kraken (pubblico) | crypto | quote, bars, stream | no | sì |
| CoinGecko | crypto | quote, search, fundamentals | no / demo | sì |
| BCE / Frankfurter | forex | fx | no | sì |
| Indicatori (interno) | tutti | SMA, EMA, RSI, MACD, Bollinger, ATR, VWAP | — | sì |
| Alpaca Market Data | USA, crypto | quote, bars, stream, news | sì | no |
| Finnhub | USA, UE | quote, stream, news, fundamentals, calendar | sì | no |
| Twelve Data | UE, forex, ETF | quote, bars, stream | sì | no |
| Revolut X | crypto | quote (solo dati) | sì | no |

### Router
- Priorità per (classe di asset, capacità), configurabili da UI; fallback in caso di errore, timeout o rate limit.
- Cache Redis: quote 1-5s, bars da minuti a ore in base al timeframe, fondamentali 24h, news 5 min.
- Rate limit con token bucket su Redis per connettore.
- Health check periodico; stato visibile in dashboard.

### Streaming
- Simboli seguiti = posizioni aperte ∪ ordini in attesa ∪ alert ∪ watchlist.
- Il worker mantiene le connessioni WebSocket; i tick vanno su Redis (`ticks:{symbol}` pub/sub + ultima quota in una hash).
- In caso di disconnessione: polling di riserva ogni 5s e riconnessione con attese crescenti.

### `BrokerAdapter`
`place_order`, `cancel_order`, `get_order`, `fills` (stream o polling), `get_positions`. Implementazioni: Alpaca Paper e Trading 212 Demo. Riconciliazione periodica delle posizioni, con le differenze mostrate in UI.

## 9. Server MCP

Endpoint `http://api:8000/mcp`, trasporto Streamable HTTP, `Authorization: Bearer <token>`. Ogni chiamata è filtrata sui portafogli dell'agente e registrata in `mcp_audit_log`.

**Portafoglio:** `list_portfolios`, `get_portfolio`, `get_positions`, `get_transactions`, `get_performance`, `get_risk_limits`.

**Ordini:** `preview_order`, `place_order`, `place_bracket_order`, `place_oco_order`, `modify_order`, `cancel_order`, `cancel_all_orders`, `get_order`, `list_orders`, `close_position`, `close_all_positions`.

**Mercato:** `search_instruments`, `get_instrument`, `get_quote`, `get_bars`, `get_market_status`, `get_indicators`, `get_fx_rate`, `get_news`, `get_fundamentals`, `get_earnings_calendar`, `get_dividends`, `screen`.

**Watchlist e alert:** `list_watchlists`, `create_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `create_alert`, `list_alerts`, `delete_alert`, `get_triggered_alerts`.

**Backtest:** `run_backtest` (strategie: `sma_cross`, `rsi_reversion`, `breakout`, `buy_and_hold`, con parametri; usa le commissioni e lo slippage del portafoglio), `get_backtest_result`.

**Risorse:** `portfolio://{id}`, `fees://schedule`, `markets://hours`.

**Errori** (codice + messaggio): `INSUFFICIENT_BUYING_POWER`, `MARKET_CLOSED_QUEUED` (informativo), `SYMBOL_NOT_ALLOWED`, `SYMBOL_NOT_FOUND`, `KILL_SWITCH_ACTIVE`, `REDUCE_ONLY`, `PORTFOLIO_LOCKED`, `STALE_QUOTE`, `RISK_LIMIT_EXCEEDED`, `SHORT_NOT_ALLOWED`, `INVALID_ORDER`, `NOT_FOUND`, `FORBIDDEN`, `RATE_LIMITED`, `PROVIDER_UNAVAILABLE`.

Gli agenti **non possono** creare portafogli, depositare, prelevare o modificare i limiti.

## 10. Web app di amministrazione

React + Vite + TypeScript, shadcn/ui + Tailwind, TanStack Query, Lightweight Charts (prezzi ed equity), Recharts (statistiche), WebSocket per gli aggiornamenti in tempo reale. Interfaccia in italiano, tema scuro e chiaro. Nome visualizzato: **SMTrading**.

Pagine:
1. **Login:** password argon2, TOTP opzionale, cookie di sessione httpOnly + CSRF.
2. **Dashboard:** equity totale in EUR, P&L del giorno, grafici dell'equity, flusso di ordini ed eseguiti, stato dei connettori, alert di margine, kill-switch.
3. **Portafogli:** elenco e creazione; dettaglio con tab Panoramica, Posizioni, Ordini (annullabili), Movimenti (filtri, CSV, storno con motivazione), Operazioni (deposito, prelievo, reset), Rischio.
4. **Agenti:** creazione, token mostrato una sola volta, rigenerazione e revoca, assegnazione dei portafogli con i permessi, log MCP per agente.
5. **Commissioni:** editor delle regole e simulatore.
6. **Rischio e margine:** profili, soglie di margine, tassi di interesse, soglie delle quote vecchie.
7. **Connettori:** attivazione, chiavi (oscurate), test, rate limit, priorità con drag & drop, stato dello streaming.
8. **Broker:** configurazione Alpaca Paper e T212 Demo, riconciliazione.
9. **Mercati:** ricerca dei simboli, quote in tempo reale, orari.
10. **Log MCP:** chiamate con filtri.
11. **Impostazioni:** fuso orario, backup ed export, verifica della catena di hash.

## 11. Deployment

- `docker-compose.yml`: `postgres:16`, `redis:7`, `api`, `worker`, `web`; rete `smtrading-net` (nome fisso, così container esterni come Paperclip possono collegarsi).
- `api` e `worker` usano la stessa immagine Python 3.12 (dipendenze con `uv`); la migrazione Alembic gira all'avvio di `api`.
- Volumi: `pgdata`, `backups`.
- `.env` / `.env.example`: `SMT_MASTER_KEY`, `SMT_ADMIN_EMAIL`, `SMT_ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `SMT_TZ`.
- Seed: admin, connettori gratuiti attivi, regola di commissione globale di default (es. 1 € + 0,1%, min 1 €, max 20 €; cambio 0,25%), profilo di rischio di default, portafoglio "Demo" da 100.000 € assegnato all'agente "paperclip-demo" (il token viene stampato nei log al primo avvio).
- Collegamento di Paperclip: `docker network connect smtrading-net <paperclip-container>`, poi configurazione dell'MCP con URL `http://api:8000/mcp` e il token.
- Backup giornaliero automatico di Postgres nel volume `backups`.

## 12. Errori e affidabilità

- Ogni operazione che tocca i soldi = una transazione Postgres + lock sul portafoglio.
- `client_order_id` univoco per portafoglio → idempotenza.
- Al riavvio il worker ricarica dal database gli ordini in attesa e i simboli seguiti; gli stop già superati durante il riavvio vengono eseguiti alla prima quota disponibile (mai a un prezzo migliore del reale).
- I job periodici sono idempotenti per (job, data) tramite la tabella `job_runs`.
- Log in JSON, `/health` per `api` e `worker`, stato visibile in dashboard.

## 13. Test

- **Unit:** commissioni, prezzi di esecuzione, trigger di stop, limit e trailing, margine, liquidazione, P&L, risoluzione delle regole.
- **Property-based (Hypothesis):** invarianti del registro (somma zero, saldi coerenti, catena di hash valida).
- **Integrazione (testcontainers Postgres e Redis):** ciclo completo di ogni tipo di ordine, OCO e bracket, ordini in coda fuori orario, margin call, interessi, dividendi, storno, kill-switch.
- **Connettori:** risposte registrate con VCR; fallback e rate limit con fornitori finti.
- **MCP end-to-end:** client MCP reale contro lo stack in Docker, che chiama ogni metodo.
- **Web app:** Playwright (login, creazione di un portafoglio, deposito, regola di commissione, kill-switch).

## 14. Ordine di realizzazione (per il piano)

1. Scheletro del repo, Docker Compose, configurazione, database e migrazioni.
2. `ledger` + `instruments` + `fees`.
3. `marketdata` con i connettori gratuiti, router, cache, streaming crypto.
4. `engine` + `risk` (market e limit, poi stop, trailing, OCO, bracket, poi short, margine, liquidazione).
5. Server `mcp` con autenticazione e audit.
6. API `admin` + web app.
7. Connettori con chiave (Alpaca, Finnhub, Twelve Data, Revolut X dati).
8. `corporate` (interessi, dividendi, split), alert, watchlist, screener.
9. `brokers` (Alpaca Paper, T212 Demo) + riconciliazione.
10. `backtest`.
11. Collegamento di Paperclip ed end-to-end.
