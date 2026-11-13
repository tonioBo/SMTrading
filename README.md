# SMTrading

Simulatore di trading per agenti AI: backend FastAPI (API admin, MCP, worker), Postgres,
Redis e l'app web di amministrazione in italiano, tutto in `docker compose`.

## Avvio

1. Copia `.env.example` in `.env` e compila i valori. `SMT_ADMIN_PASSWORD` deve avere
   **almeno 10 caratteri**: con una password più corta l'admin non viene creato.
2. `docker compose up -d --build`
3. Dal PC stesso: <http://127.0.0.1:3000/> (ingresso locale, http).

L'API (`127.0.0.1:8000`, anche per l'MCP) resta raggiungibile solo dal PC.

## Accesso dalla rete locale

Dagli altri dispositivi della rete l'app è servita **solo in https**:
<https://192.168.30.7/> (l'indirizzo http `http://192.168.30.7/` reindirizza a https).

- **Indirizzi ammessi.** L'app risponde solo agli indirizzi elencati in
  `SMT_PUBLIC_HOSTS` nel file `.env` (IP o nomi separati da virgola, predefinito
  `192.168.30.7`), oltre a `localhost`/`127.0.0.1`. Se l'IP del PC cambia, aggiorna
  `SMT_PUBLIC_HOSTS` e riavvia con `docker compose up -d web`: il certificato viene
  rigenerato con lo stesso certificato di autorità, quindi non va reinstallato.
- **Certificato.** Al primo avvio il container `web` crea una piccola autorità di
  certificazione locale ("SMTrading CA locale") e il certificato del server; sono salvati
  nel volume `web_certs`. Finché il certificato dell'autorità non è installato il browser
  mostra un avviso di sicurezza. Per installarlo scarica
  <http://192.168.30.7/smtrading-ca.crt> dal dispositivo e:
  - **Windows:** doppio clic sul file → *Installa certificato* → *Computer locale* →
    *Colloca tutti i certificati nel seguente archivio* → **Autorità di certificazione
    radice attendibili** → Fine. Riavvia il browser (Firefox: *Impostazioni → Privacy e
    sicurezza → Certificati → Mostra certificati → Autorità → Importa*).
  - **Android:** *Impostazioni → Sicurezza → Crittografia e credenziali → Installa un
    certificato → Certificato CA* e scegli il file scaricato (il percorso esatto varia
    con il produttore).
  - **iOS / iPadOS:** apri il file con Safari e consenti il download del profilo, poi
    *Impostazioni → Profilo scaricato → Installa*; infine *Impostazioni → Generali →
    Info → Impostazioni certificati attendibili* e attiva **SMTrading CA locale**.
  
  Installa solo il certificato scaricato dal tuo PC: chi possiede la chiave di questa
  autorità (nel volume `web_certs`) può emettere certificati considerati validi dai
  dispositivi che l'hanno installata.
- **Firewall di Windows.** Consenti in ingresso le porte TCP **80** e **443** solo per le
  reti private, ad esempio da PowerShell come amministratore:

  ```powershell
  New-NetFirewallRule -DisplayName "SMTrading web" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow -Profile Private
  ```

  Con Docker in WSL2 potrebbe servire anche l'inoltro delle porte dall'host a WSL
  (Docker Desktop lo fa da sé).
- **Accesso.** Dalla rete locale i tentativi di login sono limitati per indirizzo
  (nginx) e i tentativi falliti sono contati separatamente da quelli dell'ingresso locale,
  così un dispositivo in rete non può bloccare l'accesso dal PC. Le sessioni durano al
  massimo 24 ore (12 ore senza attività); da *Impostazioni* puoi chiudere tutte le altre
  sessioni e attivare l'autenticazione a due fattori.

## Funzioni

### Connettori dati (pagina *Connettori*)

Di base sono attivi i provider gratuiti (Yahoo, Kraken, Binance, CoinGecko, Frankfurter).
Per dati migliori si possono attivare i connettori a chiave:

| Connettore | Dati |
|---|---|
| Alpaca (market data) | azioni USA (feed `iex` di default), barre, news |
| Finnhub | quotazioni, news, fondamentali, calendario utili (USA/UE) |
| Twelve Data | azioni UE, forex, barre |
| Revolut X | solo dati crypto, nessun ordine |

Per abilitarne uno: *Connettori*, apri la scheda, inserisci le chiavi (salvate cifrate e
mostrate mascherate), attiva l'interruttore e, se serve, imposta il limite di richieste al
minuto. Nella sezione *Priorità dei provider* si ordina, per ogni classe di attività e
capacità (quotazioni, barre, streaming, news, ...), quale provider provare per primo: se
uno fallisce passa al successivo. I connettori disattivati o senza chiave sono saltati.

Freschezza dei dati (indicativa):

| Mercato | Fonte tipica | Freschezza |
|---|---|---|
| Crypto | Kraken | tempo reale (streaming) |
| Azioni USA, forex | Yahoo (o provider a chiave) | quasi in tempo reale |
| Azioni UE | Yahoo | ritardo di circa 15 minuti |

Protezione: se la quotazione è più vecchia della soglia della classe (*Rischio →
Quotazione vecchia*, secondi; predefiniti USA 120, UE 1200, crypto 60, forex 600) gli
ordini sono rifiutati con `STALE_QUOTE`.

### Broker paper/demo (pagina *Broker*)

Un portafoglio può replicare gli ordini su un conto **paper/demo** (Alpaca Paper,
Trading 212 Demo). Gli adattatori accettano solo gli indirizzi paper/demo: non esiste
alcun collegamento a conti reali.

1. *Broker → Nuovo collegamento*: scegli il tipo e inserisci le chiavi del conto
   paper/demo; *Verifica* controlla la connessione.
2. Assegna il collegamento a un portafoglio (non archiviato, senza ordini simulati aperti).
   *Scollega* lo toglie; non si può cambiare il collegamento finché ci sono ordini
   in corso sul broker.

Funzionamento:

- L'invio al broker è **asincrono** (processo `BrokerSync` del worker): il motore registra
  l'intenzione e lo stato dell'ordine sul broker evolve `to_send` → `sent`; un annullamento
  passa da `cancel_requested` finché il broker non lo conferma; `unknown` indica un invio
  di cui non si conosce l'esito. Un rifiuto del broker arriva quindi dopo, come ordine
  rifiutato più una notifica.
- Trading 212 non ha un riferimento d'ordine: dopo un timeout l'ordine resta `unknown` e
  **non viene mai rinviato in automatico**; BrokerSync cerca l'ordine tra quelli recenti
  del broker (simbolo, quantità, lato, finestra temporale) e, se non lo trova, lo
  rifiuta e lo lascia alla riconciliazione. Alpaca (idempotente) lo reinvia in sicurezza.
- Su un portafoglio collegato **nulla si esegue localmente**: i fill arrivano dal broker.
  Il trailing stop è simulato e al trigger diventa un ordine market reduce-only inviato al
  broker; la liquidazione per margine è instradata al broker; gli altri ordini non
  instradabili sono rifiutati all'inserimento.
- *Reset* di un portafoglio collegato: rifiutato se ci sono posizioni aperte (chiudile
  prima); gli annullamenti richiesti vengono mantenuti.
- *Riconciliazione* (*Riconcilia ora*, e periodica): confronta le posizioni del broker con
  le nostre e registra le differenze; non corregge nulla.

### Watchlist, alert e backtest

Disponibili per gli agenti via MCP (vedi `docs/paperclip-mcp.md`): watchlist (quotazioni
seguite in streaming), alert di prezzo e di variazione percentuale (arrivano come notifiche
`price_alert`), backtest di strategie solo long (`sma_cross`, `rsi_reversion`, `breakout`,
`buy_and_hold`) su barre storiche, senza toccare portafogli né broker.

### Catalogo strumenti (pagina *Mercati*)

Il catalogo parte con circa 400 strumenti (USA, Europa `.MI/.DE/.PA`, crypto in USD, forex).
La colonna *Seguito da* mostra quali agenti hanno lo strumento in una watchlist. Con
l'**aggiunta automatica** attiva (interruttore in *Mercati*) un agente che usa un simbolo
non in catalogo lo aggiunge se supera i controlli (massimo 25 aggiunte al giorno per
agente); spenta, un simbolo sconosciuto è `SYMBOL_NOT_FOUND`. L'origine di ogni strumento
è visibile in tabella.

### Archivio portafogli

*Portafogli → Archivia* congela un portafoglio (serve non avere ordini aperti): resta nel
ledger, ma sparisce da elenchi, dashboard ed esportazioni, e agli agenti appare
inesistente (`NOT_FOUND`); le assegnazioni restano. *Mostra archiviati* + *Ripristina* lo
riattiva. Non si possono inserire o modificare ordini su un portafoglio archiviato.

### Agenti e Paperclip

Token e permessi si gestiscono in *Agenti* (il token è mostrato una sola volta; *Rigenera
token* ne emette uno nuovo). Collegamento a Paperclip: `docs/paperclip-mcp.md`.

### Backup

Il servizio `backup` salva ogni giorno un `pg_dump` compresso nel volume `backups`
(`/backups/smtrading-AAAA-MM-GG.sql.gz`, ultimi 14 giorni). Conserva anche `.env`:
`SMT_MASTER_KEY` serve a decifrare le chiavi di connettori e broker.

## Test

- Backend: `cd backend && uv run pytest -q && uv run ruff check .`
- Web: `cd web && npm test -- --run && npm run lint && npm run build`
- End-to-end: `bash scripts/e2e.sh` avvia uno stack separato (progetto
  `smtrading-e2e`, con volumi, rete e credenziali proprie, web su `127.0.0.1:3190`), esegue
  Playwright e lo elimina alla fine; lo stack principale non viene toccato.
