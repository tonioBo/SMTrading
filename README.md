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
Trading 212 Demo). Gli adattatori paper/demo accettano solo gli indirizzi paper/demo e
rifiutano quelli di produzione; i conti con denaro vero hanno una procedura separata
(sezione *Denaro reale* qui sotto).

1. *Broker → Nuovo collegamento*: scegli il tipo e inserisci le chiavi del conto
   paper/demo; *Verifica* controlla la connessione.
2. Assegna il collegamento a un portafoglio (non archiviato, senza ordini simulati aperti
   e **senza posizioni aperte**: il broker non conosce le posizioni simulate).
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
  instradabili sono rifiutati (subito o, se il broker non riconosce lo strumento, poco
  dopo con una notifica). Con il collegamento disattivato gli ordini sono rifiutati con
  «Collegamento al broker disattivato» (il portafoglio resta in modalità *paper*).
- *Reset* di un portafoglio collegato: rifiutato se ci sono posizioni aperte (chiudile
  prima); gli annullamenti richiesti vengono mantenuti.
- *Riconciliazione* (*Riconcilia ora*, e periodica): confronta le posizioni del broker con
  le nostre e registra le differenze; non corregge nulla.

### Denaro reale (pagina *Broker*, sezione «Denaro reale»)

> **Stato: costruito, non attivato.** Il codice per operare con denaro vero su Alpaca
> (live) e Trading 212 (Invest) è pronto e testato solo contro un broker finto; **non
> esiste nessun collegamento live** e nessuna chiave reale è salvata. L'attivazione è
> un'azione futura dell'utente, con la checklist qui sotto.

Un portafoglio collegato a un conto reale mostra il badge rosso **🔴 REALE** (🔴 REALE ·
PROVA durante la prova a secco, 🔴 REALE · SOSPESO quando il collegamento è disattivato) e,
finché un collegamento è attivo, tutte le pagine hanno la barra rossa «Denaro reale attivo
su N portafogli». Gli ordini reali sono **autonomi** (nessuna approvazione per ordine): le
difese sono i limiti del profilo di rischio, il tetto di liquidità, il fusibile di
frequenza (`max_live_orders_per_hour`, default 30 ordini/ora per portafoglio, poi
reduce-only), il kill switch e una notifica Telegram per ogni ordine reale (sempre in coda,
mai nel riepilogo, silenziosa di notte).

Stati di un collegamento: `configured` (chiavi salvate, nessun invio) → `dry_run` (prova a
secco: gli ordini passano da rischio e invio ma si fermano prima del broker, le letture
sono vere) → `armed` (denaro reale). Chiavi sostituite o conto diverso al test → di nuovo
`configured` (serve un nuovo TOTP).

#### Cosa deve fare l'utente al momento dell'attivazione

1. **Aprire il conto.** Alpaca: domanda live da non residente (W-8BEN, KYC; verificare che
   l'Italia sia accettata ed eventualmente la cripto). Trading 212: conto Invest. Serve un
   conto **dedicato** a SMTrading, senza posizioni né ordini aperti (test, attivazione e
   collegamento rifiutano un conto non vuoto); per Alpaca un conto *cash* (niente margine).
2. **Versare** l'importo desiderato dall'app del broker.
3. **Creare le chiavi API live.** Trading 212: permessi minimi (dati del conto, metadati,
   portafoglio, ordini lettura ed esecuzione, storico; **niente «Pies - Write»**) e, se
   possibile, la restrizione all'IP pubblico del server. Alpaca: le chiavi live, distinte
   da quelle paper.
4. In SMTrading: attivare il **TOTP** dell'admin (*Impostazioni*) e verificare che
   **Telegram** sia attivo, inviando un messaggio di prova (l'attivazione richiede una prova
   riuscita nelle ultime 24 ore).
5. **Broker → Denaro reale → Nuovo collegamento reale**: incollare le chiavi e lanciare il
   **test in sola lettura** (valido 15 minuti). Controllare conto (mascherato), valuta,
   liquidità, zero posizioni e zero ordini aperti.
6. Creare o scegliere un **portafoglio vuoto** con: profilo di rischio con limite per
   ordine e perdita giornaliera massima, short non consentito; leva 1; **valuta uguale a
   quella del conto** (anche per Alpaca, in USD); deposito nel ledger pari al budget e non
   superiore alla liquidità del broker.
7. Facoltativo ma consigliato: la **prova a secco** per qualche giorno (collegando il
   portafoglio con il TOTP), per vedere su Telegram cosa avrebbero fatto gli agenti.
8. **«Attiva denaro reale»**: leggere l'avviso, spuntare le tre conferme, scrivere il nome
   del collegamento e inserire il TOTP. Poi **collegare il portafoglio** (TOTP), se non è
   già collegato.
9. **Primo collaudo** con un budget piccolo: un acquisto minimo a mercato in orario
   regolare. Verificare il messaggio Telegram, l'eseguito nel ledger, la posizione al
   broker e una riconciliazione «ok».
10. Tenere a mente i comandi di emergenza (sotto).

#### Comandi di emergenza

- **«Disattiva denaro reale»**: un clic, senza conferma né TOTP, in cima alla scheda del
  collegamento (dalla barra rossa: *Disattiva*). Il collegamento torna `configured` (da quel
  momento il cancello rifiuta ogni nuovo invio), gli ordini mai inviati sono rifiutati e
  per quelli già al broker parte la richiesta di annullamento. Se la sistemazione
  degli ordini non riesce, la scheda lo dice e il pulsante diventa «Ripeti disattivazione /
  sistema ordini».
- **Kill switch** (intestazione): sui portafogli reali blocca **ogni** invio, anche le
  chiusure; passano solo le cancellazioni e gli ordini di «Chiudi tutte le posizioni reali».
- **Da riga di comando**, anche senza interfaccia web:
  `docker compose exec -T api smtrading live disarm "<nome collegamento>" --reason "<motivo>"`
  (nessun TOTP); `docker compose exec -T api smtrading live status` elenca i collegamenti.
- **«Chiudi tutte le posizioni reali»** (dettaglio del portafoglio, TOTP): ordini a mercato
  reduce-only, ammessi anche con il kill switch acceso.
- L'**app del broker** resta sempre l'ultima risorsa.

#### Sorveglianza e reduce-only automatico

Il worker riconcilia posizioni e liquidità ogni 60 s e dopo ogni eseguito (non corregge mai
nulla), controlla l'impronta del conto e la salute del collegamento. Conto cambiato dietro
le stesse chiavi (il collegamento torna anche `configured`), differenze di posizioni o
liquidità confermate da due passaggi consecutivi, conto a margine, esito sconosciuto di un
ordine e fusibile portano il portafoglio in **reduce-only** con un avviso 🔴; broker
irraggiungibile o credenziali rifiutate danno un avviso 🔴. Un reduce-only
con motivo `live_*` si toglie solo con «Ho verificato, riprendi» (TOTP): né l'azzeramento
di fine giornata né la modifica dello stato del portafoglio lo tolgono.

#### Registro denaro reale

La pagina *Registro denaro reale* (`/registro-denaro-reale`, esportabile in CSV) elenca ogni
azione sui collegamenti reali (chiavi salvate, test, prova a secco, attivazione,
disattivazione, collegamento, ripresa, chiusura). La tabella `live_audit` è **append-only**
grazie a un trigger del database che rifiuta `UPDATE`, `DELETE` e `TRUNCATE`. Limiti: il
trigger non protegge da chi ha i diritti di proprietario della tabella o di superutente
PostgreSQL (può disattivare o eliminare il trigger, impostare
`session_replication_role = replica`, eliminare la tabella o fare il downgrade della
migrazione), né da un ripristino di un backup più vecchio; non impedisce l'inserimento di
righe nuove da parte di chi ha accesso al database. È una protezione contro errori e
codice applicativo, non contro chi controlla il server.

#### Limiti noti

- **Conferma degli ordini reali dall'interfaccia:** il componente `LiveOrderConfirm`
  («Ordine con denaro reale», un clic) esiste ma non è collegato a nulla: il portale non ha
  ancora un modulo per inserire ordini a mano; oggi gli ordini reali arrivano solo dagli
  agenti (e da «Chiudi tutte le posizioni reali»).
- **Prezzo di riserva del tetto di liquidità:** senza quotazione si usa l'ultimo eseguito
  del portafoglio sullo stesso simbolo (×1,02) se non è più vecchio di «un giorno di
  borsa»: 24 ore, 72 ore di sabato, domenica e lunedì (UTC). La regola **non conosce le
  festività**: dopo un giorno festivo l'eseguito può risultare scaduto e l'ordine viene
  rifiutato finché non c'è una quotazione.
- **Broker finto solo per i test:** l'impostazione `SMT_LIVE_FAKE_BROKER` (spenta di
  default) sostituisce i broker reali con un broker in memoria; la usa solo lo stack e2e
  (`scripts/e2e.sh`). Quando è accesa, API e worker scrivono all'avvio un avviso nei log:
  non va mai attivata nello stack principale. Il seed e2e
  (`python -m smtrading.devtools.live_e2e_seed`) si rifiuta di partire se è spenta.
- Trailing stop simulati (dipendono dal worker), dividendi e corporate action reali non
  importati (differenze di liquidità → reduce-only), API Trading 212 in beta: vedi
  «Rischi aperti» nello spec `docs/superpowers/specs/2026-11-14-real-trading-design.md`.

**Nota fiscale:** con Alpaca e Trading 212 si applica di norma il regime dichiarativo (quadri
RT, RM, RW): SMTrading conserva lo storico ma non calcola imposte. Vedi la «Nota fiscale»
dello spec `docs/superpowers/specs/2026-11-14-real-trading-design.md` e verificare con un
commercialista.

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

*Portafogli → Archivia* congela un portafoglio (serve non avere ordini aperti, non essere
collegato a un broker e non avere posizioni allo scoperto né prestiti a margine, perché da
archiviato non matura interessi): resta nel ledger, ma sparisce da elenchi, dashboard ed
esportazioni, e agli agenti appare inesistente (`NOT_FOUND`); le assegnazioni restano.
*Mostra archiviati* + *Ripristina* lo riattiva: prima applica i dividendi e i frazionamenti
avvenuti mentre era archiviato (se il calendario non è leggibile il ripristino è rifiutato,
riprova più tardi). Non si possono inserire o modificare ordini su un portafoglio
archiviato.

### Blocco aperture

Il CEO (o un admin) può sospendere le **nuove aperture**, su tutti i portafogli o su uno
solo: finché il blocco è attivo ogni ordine che aumenterebbe l'esposizione (nuove posizioni,
incrementi, short, ingressi di bracket/OCO) è rifiutato con `TRADING_HALTED`
(«Aperture sospese dal CEO: <motivo>»); chiusure, riduzioni, stop loss/take profit delle
posizioni esistenti, annullamenti, liquidazioni e sincronizzazione broker continuano. Vale per
portafogli simulati, paper e reali, con lo stesso controllo del kill switch.

- L'agente CEO usa `set_trading_halt` / `get_trading_halt` (vedi `docs/paperclip-mcp.md`):
  serve il permesso `halt`, da concedere solo al CEO dalla scheda dell'agente oppure con
  `docker compose exec api python -m smtrading.cli agent halt-permission "CEO SMTrading" on`.
- Nella web app un banner su Dashboard e dettaglio portafoglio mostra motivo, autore e ora;
  la scheda **Rischio → Blocco aperture** permette di attivarlo, revocarlo (**Revoca**) e
  vedere lo storico (le righe non vengono mai cancellate).
- Attivazione e revoca arrivano su Telegram come messaggi critici.

### Notifiche Telegram (pagina *Notifiche*)

SMTrading scrive da solo in un gruppo Telegram, senza passare da Paperclip.

**Collegamento.**

1. Su Telegram apri @BotFather, `/newbot`, scegli nome e username e copia il token.
2. Crea un gruppo, aggiungi il bot e scrivi `/start@nome_bot` nel gruppo: con la privacy
   del bot attiva (impostazione predefinita) i messaggi normali non gli arrivano. Telegram
   conserva questi aggiornamenti solo per circa 24 ore: se il gruppo non compare, riscrivi
   il comando.
3. *Notifiche → Collegamento*: incolla il token (resta cifrato e non viene mai mostrato:
   solo `••••` e `@nome_bot`), poi **Rileva gruppi** elenca i gruppi in cui il bot ha
   ricevuto messaggi; sceglilo (o inserisci l'ID a mano). Ogni modifica è verificata con
   Telegram (`getMe`/`getChat`) e salvata solo se valida.
4. Attiva le notifiche e premi **Invia messaggio di prova**.

Da riga di comando (il token si legge solo da stdin, mai dagli argomenti, e non viene stampato):

```bash
docker compose exec -T api python -m smtrading.cli telegram set --chat-id <id> --token-stdin --enable < token.txt
docker compose exec api python -m smtrading.cli agent notify NOME on   # oppure off
```

`telegram set` accetta anche `--no-verify`; senza `--token-stdin` cambia solo il gruppo.
`agent notify` concede o toglie all'agente (di solito il Notifier) il permesso di usare
`send_telegram` (vedi `docs/paperclip-mcp.md`); si cambia anche dalla scheda dell'agente.

**Cosa invia SMTrading direttamente** (messaggi in testo semplice):

| Livello | Eventi |
|---|---|
| 🔴 critico | kill switch attivato, blocco aperture attivato o revocato, liquidazione, perdita giornaliera, ordine sconosciuto al broker |
| 🟠 avviso | ordine rifiutato, kill switch disattivato, avviso di margine, liquidazione in attesa del broker, prezzo di esecuzione mancante, modifica ordine non riuscita, differenze di riconciliazione broker |
| 🟢 eseguito | ordine eseguito |

Il resto (ordini accettati o annullati, alert di prezzo, decisioni) compare solo nel feed
delle attività e nei riepiloghi.

**Ore silenziose e riepiloghi** (fuso orario del server):

- dalle 23:00 alle 07:00 i messaggi critici arrivano comunque, senza suono; gli altri sono
  raccolti e inviati alle **07:00** in un unico *Riepilogo della notte* (gli eventi di un
  portafoglio archiviato nel frattempo sono esclusi, come nei riepiloghi);
- **18:00** riepilogo giornaliero e **venerdì 18:30** riepilogo settimanale, con la
  classifica dei portafogli (rendimento, drawdown massimo, numero di operazioni) e le
  decisioni del periodo. Sono costruiti da SMTrading e arrivano anche se Paperclip è fermo;
  un riepilogo mancato per un riavvio è inviato al ritorno del worker, ma solo prima delle
  23:00 dello stesso giorno (se a quell'ora Telegram non è pronto, il log del worker lo
  segnala con un WARNING).

**Pausa, disattivazione, coda e ritentativi.** Ogni messaggio passa dalla coda `outbox`
(*Notifiche → Ultimi messaggi*): `pending`, `sent`, `paused`, `failed`, `skipped`.
*Metti in pausa* ferma gli invii: i messaggi restano in coda e all'uscita dalla pausa
partono in ordine, tranne quelli non critici in pausa da più di 24 ore, che diventano
`skipped` (notizie vecchie; i critici partono sempre); la pausa non blocca il messaggio di
prova. **Disattivare** le notifiche invece scarta gli eventi diretti che avvengono mentre
sono spente: non vengono inviati nemmeno dopo la riattivazione (restano nel feed delle
attività e nei riepiloghi). Per sospendere temporaneamente senza perdere nulla usa la
pausa. Un messaggio `failed` si rimanda con *Riprova*, tutti insieme con *Riprova tutti i
falliti* (i messaggi di prova esclusi).

**Interruzioni di Telegram.** Gli invii sono uno alla volta, in ordine, a distanza di
almeno 3 secondi. Errori transitori del servizio (rete, 5xx, 429) non fanno mai fallire
un messaggio: si riprova con attesa crescente (5, 10, 20 … al massimo 900 s; con 429 si
attende `retry_after`) e, tornato Telegram, i messaggi arrivano nell'ordine originale.
Se il token salvato non è più leggibile (per esempio con una nuova `SMT_MASTER_KEY`) gli
eventi restano in attesa finché non lo reinserisci; gli eventi non critici più vecchi di
24 ore sono scartati, quelli critici partono comunque. Errori che dipendono dalla
configurazione (token non valido, bot rimosso o senza permessi, gruppo non trovato)
bloccano la coda: dopo 10 tentativi la testa passa a `failed` e la coda resta ferma; se
la configurazione non viene corretta anche i messaggi successivi falliscono, uno alla volta
(circa 51 minuti ciascuno). Salvare un nuovo token o un nuovo gruppo, togliere la pausa o
premere *Riprova tutti i falliti* sblocca subito la coda (senza attendere i 900 s); i
messaggi già `failed` si rimandano con *Riprova* o *Riprova tutti i falliti*. Un errore
specifico di un messaggio (es. 400) lo fa fallire da solo senza fermare gli altri. Se il
gruppo diventa supergruppo l'ID viene aggiornato da solo. Con Telegram non configurato o
disattivato gli eventi diretti sono scartati (vedi sopra), i messaggi già in coda
aspettano e i riepiloghi non partono.

### Agenti e Paperclip

Token e permessi si gestiscono in *Agenti* (il token è mostrato una sola volta; *Rigenera
token* ne emette uno nuovo). Collegamento a Paperclip: `docs/paperclip-mcp.md`.

### Backup

Il servizio `backup` salva ogni giorno un `pg_dump` compresso nel volume `backups`
(`/backups/smtrading-AAAA-MM-GG.sql.gz`, ultimi 14 giorni). Conserva anche `.env`:
`SMT_MASTER_KEY` serve a decifrare le chiavi di connettori e broker.

I log dei processi (`docker compose logs`) non contengono chiavi: le richieste HTTP verso i
fornitori non sono registrate e un filtro maschera token, API key, password e credenziali
`Authorization` in ogni riga.

### Aggiornamenti

Dopo un `docker compose up -d --build` una scheda aperta con la versione precedente mostra
«Nuova versione disponibile — Ricarica». Per un identificativo esplicito della build:
`SMT_BUILD_ID=$(git rev-parse --short HEAD) docker compose up -d --build` (altrimenti
l'immagine web usa l'ora della build).

## Test

- Backend: `cd backend && uv run pytest -q && uv run ruff check .`
- Web: `cd web && npm test -- --run && npm run lint && npm run build`
- End-to-end: `bash scripts/e2e.sh` avvia uno stack separato (progetto
  `smtrading-e2e`, con volumi, rete e credenziali proprie, web su `127.0.0.1:3190`), esegue
  Playwright e lo elimina alla fine; lo stack principale non viene toccato. Solo quello
  stack gira con `SMT_LIVE_FAKE_BROKER=1`: dopo le altre spec lo script esegue il seed e2e
  del denaro reale e `web/e2e/live.spec.ts` (procedura completa contro il broker finto,
  chiavi «canarino», TOTP di test generato a ogni esecuzione).
