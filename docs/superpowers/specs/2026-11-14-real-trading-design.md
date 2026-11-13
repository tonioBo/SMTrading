# SMTrading — Trading con denaro reale su Alpaca e Trading 212 (design, piano 8)

Data: 2026-11-14 · Stato: bozza da approvare · Dipende da: piano 6 (broker paper/demo, modalità), piano 7 (outbox e notifiche Telegram)

## Perché
Oggi un portafoglio può essere in **simulazione** o in **paper** (conto demo del broker). La modalità **reale** (badge rosso, piano 6 Task 16) esiste solo nell'interfaccia e non viene mai prodotta. Il piano 8 costruisce il percorso verso il denaro reale su Alpaca e Trading 212, con tutte le protezioni. **Non lo attiva:** nessun conto reale viene collegato da noi. L'attivazione la fa più tardi l'utente, con le sue chiavi live e una conferma TOTP.

## Decisioni dell'utente (vincolanti, 2026-11-13)
- Broker: **Alpaca** e **Trading 212**, conti live.
- **Nessun tetto globale al capitale.** I limiti vengono da ciascun portafoglio: la sua liquidità e il suo profilo di rischio.
- Gli ordini reali sono **autonomi**: nessuna approvazione umana per singolo ordine, dentro i limiti del profilo e del rischio.
- **Una notifica Telegram per ogni ordine reale**, con l'outbox e il notificatore del piano 7.
- **Costruire ma non attivare.** La modalità «reale» diventa producibile solo per i portafogli collegati a un adattatore live abilitato con TOTP.

## Obiettivi
- Due adattatori live distinti, `alpaca_live` e `t212_live`, con gli URL di produzione scritti solo nelle loro classi.
- Un flusso di attivazione per collegamento:
  1. chiavi;
  2. test in sola lettura;
  3. prova a secco (facoltativa);
  4. «Attiva denaro reale» confermato con TOTP.
- Disattivazione con un clic e senza TOTP.
- Protezioni automatiche:
  - kill switch;
  - stato reduce-only;
  - riconciliazione frequente;
  - liquidità del portafoglio come tetto;
  - avvisi critici su Telegram.
- Tutto testabile senza conti reali.

## Non-obiettivi
- Collegare un conto reale, o anche solo salvare chiavi live, durante lo sviluppo o i test.
- Approvazione manuale per ordine, e tetto globale al capitale (esclusi per decisione dell'utente).
- Margine, leva e vendite allo scoperto in modalità reale: il portafoglio reale è sempre a leva 1, senza short.
- Opzioni, derivati, CFD e pie di Trading 212.
- Depositi e prelievi verso il broker: si fanno dall'app del broker.
- Calcolo delle imposte o produzione di dichiarazioni (vedi la nota fiscale).
- Ordini da Telegram.
- Più portafogli SMTrading sullo stesso conto reale.

## Anti-obiettivi
- Un adattatore paper/demo che, cambiando l'URL nella configurazione, parla con la produzione (Ruling R4 resta valido).
- Una chiave live in chiaro in log, errori, risposte API, audit o messaggi Telegram.
- Un ordine reale inviato due volte. Trading 212 non è idempotente (Ruling R7): non si reinvia mai in automatico.
- Un ordine reale inviato senza che l'utente ne sia informato su Telegram.
- Un ordine reale che usa più liquidità di quella del portafoglio SMTrading.

---

## Ricerca (fonti ufficiali, verificate il 2026-11-13)

### Alpaca
- **Apertura del conto per residenti in Italia: non confermata, ma nemmeno esclusa.**
  - Alpaca dichiara servizi in «195+ paesi» per i non residenti negli USA ed esclude esplicitamente il Canada. La procedura prevede:
    - conto paper con MFA;
    - richiesta live;
    - codice fiscale del paese di residenza;
    - modulo **W-8BEN**;
    - KYC con selfie;
    - deposito minimo di 1 $.
  - Fonte: [How to Open a Live Trading Account as a Non-US Resident](https://alpaca.markets/learn/live-trading-account-non-us), aggiornata a marzo 2026.
  - L'elenco dei paesi non è pubblico: la pagina [Countries Alpaca is available](https://alpaca.markets/support/countries-alpaca-is-available) (febbraio 2026) rimanda al supporto.
  - **Va verificato dall'utente facendo domanda.** Se il conto non viene approvato, l'adattatore `alpaca_live` resta inutilizzato e Trading 212 diventa l'unica via.
- **Il «passaporto» UE di luglio 2026 riguarda i partner B2B, non il conto retail.** [L'annuncio](https://alpaca.markets/blog/alpaca-completes-eea-passporting-to-29-countries-expanding-access-to-regulated-investment-services-across-europe/) del 7 luglio 2026 parla di passaporto in 29 paesi SEE, Italia compresa. È rivolto a «fintechs, financial institutions, and enterprise partners» e passa da WealthKernel Spain (CNMV n. 328). Riguarda la Broker API. Noi usiamo la **Trading API** di un conto individuale (Alpaca Securities LLC, USA).
- **API live:**
  - stessi endpoint del paper: «The API spec is the same between the paper trading and live accounts»;
  - chiavi diverse dal conto paper;
  - URL base live `https://api.alpaca.markets`, quello paper è `https://paper-api.alpaca.markets`.
  - Differenze del live: slippage reale, dividendi, commissioni regolamentari e costi di prestito titoli.
  - Fonte: [Paper Trading](https://docs.alpaca.markets/docs/paper-trading).
- **Frazioni e importi:**
  - `qty` o `notional`, mai entrambi;
  - `time_in_force=day` per le frazioni;
  - importi in USD con al massimo 2 decimali.
  - Fonte: [Fractional Trading](https://docs.alpaca.markets/docs/fractional-trading). La pagina è in parte contraddittoria sulle frazioni in orario esteso: in reale ammettiamo frazioni solo in orario regolare.
- **Cripto per residenti UE: incerta.**
  - La cripto è fornita da Alpaca Crypto LLC, un MSB registrato FinCEN negli USA ([crypto disclosures](https://alpaca.markets/disclosures/crypto-disclosures)). Le [docs](https://docs.alpaca.markets/docs/crypto-trading) parlano di «select international jurisdictions».
  - Dal 1° luglio 2026 il periodo transitorio MiCA è finito, e chi non ha licenza non può servire residenti UE.
  - → In `alpaca_live` la cripto è **disattivata di default**. Si abilita solo se il test del conto mostra la cripto attiva (`crypto_status` del conto) e l'utente la spunta.
- **PDT:**
  - la pagina [The Intraday Margin Rule](https://docs.alpaca.markets/us/docs/the-intraday-margin-rule) (aggiornata il 7 luglio 2026) dice che la definizione di Pattern Day Trader è stata **eliminata**: niente soglia di 25.000 $ né conteggio dei day trade;
  - resta un regime di margine intraday con margin call.
  - Il portafoglio reale è a leva 1 senza short, quindi in pratica il tema non si pone.
  - Precauzione: se il broker rifiuta un ordine per motivi PDT o di margine, il rifiuto arriva come rifiuto normale (Ruling R9) con un avviso 🟠.
- Alpaca usa `client_order_id`, quindi è idempotente: il comportamento del piano 6 resta valido.

### Trading 212
- **Ambienti:** demo `https://demo.trading212.com/api/v0`, live `https://live.trading212.com/api/v0`, stessa API ([docs](https://docs.trading212.com/api.md)).
- L'API è in **beta**: «currently in beta and is under active development» ([Key Concepts](https://docs.trading212.com/api/section/general-information/key-concepts)).
- **Conti:** API disponibile solo per i conti **Invest** e **Stocks ISA**. L'ISA è un prodotto britannico, quindi per un residente in Italia vale solo **Invest**, niente CFD.
- **Tipi di ordine in live:**
  - storicamente la documentazione diceva che in live era supportato solo il mercato;
  - dal **29 gennaio 2026** Trading 212 annuncia: «You can now place Limit, Stop, and Stop Limit orders via the API on real-money accounts» ([community, thread API Update](https://community.trading212.com/t/trading-212-api-update/87988?page=8));
  - solo gli ordini a mercato ammettono l'orario esteso;
  - alcune pagine della documentazione mostrano ancora la limitazione.
  - → I tipi ammessi da `t212_live` sono configurabili. Il default è «mercato, limite, stop, stop-limit», e il test di attivazione mostra quali tipi sono abilitati.
- **Valuta:** «Orders can be executed only in the main account currency». Il conto multivaluta non è supportato via API, e i valori arrivano nella valuta principale del conto.
  - → Il portafoglio reale deve avere `base_currency` uguale alla valuta principale del conto.
  - Il cambio applicato da Trading 212 (commissione FX) entra nel prezzo o nella liquidità: si vede in riconciliazione.
- **Autenticazione:** HTTP Basic con `API_KEY:API_SECRET`, come nel demo già implementato.
- **Permessi della chiave:** si scelgono alla creazione (Impostazioni → API (Beta)): dati del conto, storico, ordini, portafoglio, metadati ([Help Centre](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key)).
  - Gli errori indicano scope come `orders:read` e `orders:execute` (es. 403 «Scope( orders:execute ) missing for API key»).
  - Servono: account data, metadata, portfolio, orders read, orders execute, history.
  - Non servono e vanno lasciati spenti: «Pies - Write» ed eventuali altri.
- **Restrizione IP:** facoltativa, si imposta dalle impostazioni del conto. **Consigliata** con l'IP fisso del server.
- **Limiti di frequenza:**
  - valgono **per conto**, indipendentemente da chiave e IP, quindi altri script dell'utente sullo stesso conto li consumano;
  - intestazioni `x-ratelimit-*`;
  - esempi: riepilogo conto 1 richiesta/5 s, posizioni 1/1 s, ordine per ID 1/1 s, ordine limite 1/2 s, strumenti 1/50 s, storico 20/min;
  - al massimo 50 ordini pendenti per titolo.
- Non è idempotente (Ruling R7, invariato): esito sconosciuto → niente nuovo invio.

### Nota fiscale (solo un riferimento, non è una consulenza)
Alpaca (USA) e Trading 212 (intermediario estero) di norma non fanno da sostituto d'imposta per un residente in Italia: si applica il **regime dichiarativo** e non quello amministrato. In pratica, in dichiarazione si indicano:
- capital gain nel quadro RT;
- redditi di capitale (dividendi, con la ritenuta USA ridotta dal W-8BEN) nel quadro RM;
- monitoraggio e IVAFE nel quadro RW.

Le regole e le eventuali novità vanno verificate con un commercialista e sulle istruzioni dell'[Agenzia delle Entrate](https://www.agenziaentrate.gov.it/). SMTrading conserva lo storico di ordini, eseguiti e ledger, ma non calcola imposte.

---

## Approcci considerati
1. **Classi live separate sopra un nucleo di protocollo condiviso** (scelto).
   - Il codice di `alpaca_paper.py` e `t212_demo.py` (mapping, payload, stati, `find_order`) si sposta in un nucleo per broker: `_AlpacaProtocol`, `_T212Protocol`.
   - L'URL è una `ClassVar` costante, mai letta dalla configurazione.
   - Quattro classi concrete: `AlpacaPaperAdapter` e `T212DemoAdapter` (invariate verso l'esterno), `AlpacaLiveAdapter` e `T212LiveAdapter` in `brokers/live/`.
   - Pro: una sola logica testata da paper e live; gli URL live stanno solo in `brokers/live/`.
   - Contro: un refactoring dei due adattatori esistenti, da coprire con la stessa suite.
2. **Interruttore di URL nella configurazione dell'adattatore paper.** Scartato: viola R4 e l'anti-obiettivo. Un errore di configurazione porterebbe il denaro reale in una classe nata per il demo.
3. **Adattatori live scritti da zero, duplicando il codice.** Massimo isolamento, ma con due copie che divergono: le correzioni del piano 6 (C1, R7, R11) andrebbero portate a mano. Scartato per il rischio di deriva.

**Raccomandazione: approccio 1.** Lo accompagna una guardia di rete indipendente dall'adattatore (vedi «Cancello live»), così anche un bug nel nucleo non può inviare in produzione senza che il collegamento sia armato.

---

## Design

### 1. Adattatori live
- Nuovi `kind`:
  - `alpaca_live`, `BASE_URL = "https://api.alpaca.markets"`;
  - `t212_live`, `BASE_URL = "https://live.trading212.com"`.
- Registrati in `registry._load()` da `smtrading.brokers.live`.
- `BrokerAdapter` riceve `live: ClassVar[bool] = False`. Le classi live hanno `live = True`. La docstring «Adapters never talk to real-money endpoints» diventa: «Paper/demo adapters never… Live adapters only under the live gate».
- Il costruttore di ogni classe ignora `base_url` nella configurazione. Se è presente e diverso dalla costante, rifiuta con `FORBIDDEN`. Vale anche per le classi paper, ancora più rigide: rifiutano esplicitamente gli host live.
- **Test di guardia** in CI: una scansione del sorgente fallisce se `api.alpaca.markets` o `live.trading212.com` compaiono fuori da `brokers/live/` e dai test che ne verificano il rifiuto.
- Le capacità live sono più strette di quelle paper:
  - niente short: le vendite oltre la posizione vengono rifiutate prima del rischio;
  - niente leva;
  - Alpaca:
    - crypto solo se abilitata;
    - `notional` solo a mercato e in orario regolare;
    - frazioni solo `day`;
  - Trading 212: tipi da configurazione, solo quantità (già così nel demo), valuta del conto uguale a quella del portafoglio.
- Limiti di frequenza: `t212_live` rispetta `x-ratelimit-remaining` e `x-ratelimit-reset`. Una 429 non è un rifiuto: l'ordine resta `to_send`, oppure, se l'invio è già avvenuto, resta nel percorso di R7.

### 2. Stato del collegamento live e «Cancello live»
Nuove colonne in `broker_links` (migrazione successiva al piano 7):
- `live_state`: `configured` | `dry_run` | `armed`, nullo per i collegamenti paper/demo;
- `armed_at`, `armed_by`, `disarmed_at`, `disarmed_reason`;
- `account_fingerprint`: hash dell'ID del conto broker letto al test;
- `last_account_snapshot` JSON: liquidità, valuta, tipo di conto, crypto sì/no, letto in sola lettura.

Macchina a stati:

```
(nessuna chiave) ──salva chiavi──▶ configured ──[prova a secco]──▶ dry_run
                                      ▲   │                          │
                                      │   └──── Attiva (TOTP) ───────┴──▶ armed
                                      └──────── Disattiva (1 clic, nessun TOTP) ◀──┘
```

- Una sostituzione delle chiavi, o un `account_fingerprint` diverso al test, riporta sempre a `configured`. Le nuove chiavi richiedono un nuovo TOTP.
- Il **Cancello live** sta nel client HTTP usato dagli adattatori live: un `httpx` transport dedicato per gli host live. Prima di ogni richiesta che non sia una lettura (`POST`, `DELETE`, `PATCH`, `PUT`) controlla, in una lettura breve dal database:
  - collegamento `armed`;
  - kill switch spento;
  - portafoglio collegato non `locked`.

  Altrimenti solleva `LiveGateClosed` e non apre nemmeno la connessione.
- Eccezione: in `armed` le **cancellazioni** (`DELETE`) passano sempre, anche con il kill switch acceso.
- In `dry_run` il cancello intercetta l'invio. L'ordine passa da rischio, BrokerSync e payload, poi si ferma prima del `POST`:
  - stato `rejected` con messaggio «prova a secco: ordine non inviato»;
  - payload (senza segreti) salvato in `orders.broker_raw`;
  - Telegram «🧪 REALE (prova)».

  Le letture (conto, posizioni, ordini) sono vere, così si prova tutto il percorso con il conto vero senza rischio.
- Il cancello è la seconda linea di difesa. La prima resta BrokerSync con `_must_stop`, che non reclama gli ordini se il collegamento non è armato.

### 3. Modalità del portafoglio
- `portfolio_mode()` restituisce `reale` se il portafoglio è collegato a un collegamento con `adapter_class(kind).live`.
- Il DTO aggiunge `live_state`. In UI:
  - `armed` → 🔴 REALE;
  - `dry_run` → 🔴 REALE · PROVA;
  - `configured` → 🔴 REALE · SOSPESO.
- **Collegare** un portafoglio a un collegamento live richiede che il collegamento sia `armed`, più un TOTP. In più valgono le regole di `link_portfolio`: niente ordini aperti e niente posizioni, e inoltre:
  - **1 collegamento live ↔ 1 portafoglio.** La riconciliazione di posizioni e liquidità ha senso solo su un conto dedicato.
  - Il portafoglio ha:
    - un profilo di rischio con `max_order_notional_eur` e `max_daily_loss_pct` (o `_eur`) **valorizzati**;
    - `short_allowed=false`;
    - `leverage=1`.

    Sono i limiti per portafoglio previsti dalla decisione dell'utente.
  - `base_currency` uguale alla valuta del conto (Trading 212). Per Alpaca il conto è in USD e il portafoglio può essere in EUR, con conversione FX come oggi.
  - La liquidità del portafoglio non supera quella disponibile al broker, letta dal test. La si imposta con un deposito nel ledger, scelto dall'utente: è il «budget» del portafoglio.
  - Il conto non ha posizioni né ordini aperti (conto dedicato). Se ne ha, il collegamento viene rifiutato con un messaggio chiaro. Vedi i rischi aperti.

### 4. Protezioni
- **Kill switch:** acceso, blocca **ogni invio** verso i collegamenti live, anche gli ordini che riducono la posizione. È più severo di R25, che vale per il paper.
  - Restano consentite le cancellazioni: `set_kill_switch_and_cancel` (R3) annulla gli ordini aperti al broker.
  - Le chiusure si fanno spegnendo il kill switch oppure dall'app del broker.
  - Messaggio Telegram 🔴.
- **Disattiva denaro reale:** un clic, senza TOTP. Porta il collegamento a `configured`, chiede la cancellazione degli ordini aperti al broker (le cancellazioni passano) e invia un messaggio 🔴. Gli ordini non ancora inviati del portafoglio vengono rifiutati: «denaro reale disattivato».
- **Locked / reduce-only:** valgono come oggi. In reale, reduce-only scatta da solo in questi casi, tutti con un messaggio 🔴 critico:
  - una riconciliazione con differenze;
  - un ordine con esito sconosciuto (R7 `_give_up`);
  - un `account_fingerprint` cambiato;
  - una liquidità broker inferiore a quella del portafoglio oltre la tolleranza.

  Per tornare `active` serve un'azione dell'admin con TOTP («Ho verificato, riprendi»).
- **Tetto di liquidità** (per portafoglio, nessun tetto globale):
  - al controllo pre-trade e di nuovo in BrokerSync prima dell'invio, un acquisto in reale richiede: importo stimato + commissioni stimate + margine prudenziale (default 2%, per slippage e FX) ≤ **min(liquidità disponibile del portafoglio, liquidità disponibile al broker)**;
  - la liquidità del broker viene dall'ultimo snapshot del conto, letto al massimo 60 s prima (per Trading 212, 1 lettura ogni 5 s al massimo);
  - restano validi gli altri limiti del profilo di rischio.
- **Riconciliazione:**
  - per i collegamenti live gira ogni **60 s** (oggi `RECONCILE_INTERVAL_S = 300`) e dopo ogni eseguito;
  - confronta posizioni **e liquidità**;
  - viene saltata solo per i simboli con eseguiti ricevuti ma non ancora applicati nello stesso passaggio di BrokerSync, per evitare falsi allarmi;
  - qualsiasi differenza → reduce-only + Telegram critico + riga `reconciliations`;
  - non corregge mai nulla da sola: resta l'invariante di `reconcile`.
- **Fusibile di frequenza** (proposta, non è un tetto al capitale): al massimo N ordini reali all'ora per portafoglio, default 30, configurabile nel profilo. Superato il limite → reduce-only + 🔴. Protegge da un agente in loop.
- **Trailing stop** (R10/R11): restano simulati localmente e al trigger diventano ordini a mercato reduce-only. In reale questo dipende dal worker acceso: la UI lo segnala nel modulo d'ordine («stop gestito da SMTrading, non dal broker»).
- **Salute del collegamento:** 3 errori consecutivi di `health_check` → 🔴 «broker non raggiungibile». Lo stato non cambia, perché tanto non si invia nulla.

### 5. Semantica del ledger
- La **fonte di verità degli eseguiti è il broker.** Ogni eseguito reale entra con `execute_fill(fill_qty, fill_price, source="broker")` (R2): niente prezzo da quota né slippage simulato.
- Le commissioni:
  - quelle riportate dal broker, se presenti (le commissioni regolamentari Alpaca sulle vendite, la commissione FX di Trading 212), si registrano come commissione effettiva;
  - altrimenti si stimano con le regole del piano 6, e la differenza emerge nella riconciliazione della liquidità;
  - non si inventano prezzi: R2 e il comportamento `FILL_PRICE_WAIT` restano invariati.
- R9: tutto l'I/O verso il broker resta asincrono in BrokerSync.
- R10: niente si esegue in locale su un portafoglio collegato.
- R11: un trailing scattato mantiene il proprio reduce-only e i controlli locali.
- Dividendi e corporate action reali: in questo piano non si importano. Una differenza di liquidità dovuta a un dividendo genera reduce-only e un avviso. Per sistemarla l'admin registra un movimento manuale con causale.

### 6. Segreti
- Le chiavi live vengono cifrate con `common.crypto.encrypt_json` (master key), come le paper, in un campo con contesto di dominio `broker_live` (associated data) per non confondere i blob.
- Non vengono mai restituite. L'API mostra solo `••••` + le ultime 4 cifre della key ID + l'ID conto mascherato.
- Mai nei log:
  - il logging di `httpx` per gli host live è spento;
  - i messaggi d'errore passano dalla redazione già usata per Telegram (piano 7);
  - un test verifica che chiave e segreto non compaiano in log, errori, audit e outbox.
- Sostituire le chiavi → `configured` e un nuovo TOTP.

### 7. Telegram (dal piano 7)
- **Ogni ordine reale** produce un messaggio, accodato nella `telegram_outbox` da SMTrading e non dall'agente:
  - 🔴 REALE · inviato — portafoglio, simbolo, lato, quantità o importo, tipo, agente;
  - 🔴 REALE · eseguito / parziale — prezzo, valore, commissioni;
  - 🔴 REALE · rifiutato / annullato — con il messaggio del broker.
- Nelle ore notturne del piano 7 i messaggi degli ordini reali **non** si accumulano nel riepilogo delle 07:00: partono subito, silenziosi (`disable_notification=true`).
- **Critici**:
  - attivazione e disattivazione;
  - kill switch;
  - riconciliazione con differenze;
  - esito sconosciuto;
  - reduce-only automatico;
  - fusibile di frequenza;
  - conto cambiato;
  - broker non raggiungibile.
- **Precondizione per attivare:** Telegram configurato e attivo, con un messaggio di prova riuscito nelle ultime 24 ore. Se l'invio è in pausa, l'attivazione viene rifiutata. Se Telegram si guasta dopo l'attivazione, gli ordini continuano (outbox persistente con nuovi tentativi), e la UI mostra un avviso fisso.

### 8. Audit
- Nuova tabella append-only `live_audit`: id, ts, link_id, portfolio_id?, attore (admin email / `system` / agente), azione, IP, user agent e dettagli JSON senza segreti.
- Azioni registrate:
  - `keys_saved`, `test_ok` / `test_failed`;
  - `dry_run_on`, `armed`, `disarmed`;
  - `portfolio_linked`, `reduce_only_auto`, `resumed`;
  - `kill_switch`.
- Ogni invio reale salva payload e risposta del broker, senza intestazioni, nell'ordine (`broker_raw`). Ordine e audit sono collegati.
- La UI ha una vista «Registro denaro reale», filtrabile ed esportabile in CSV.

### 9. Interfaccia
- **🔴 REALE ovunque:** badge del portafoglio, intestazione delle pagine, liste di ordini e posizioni, dashboard.
- Quando esiste almeno un collegamento `armed`, il `ModeBanner` diventa una barra rossa fissa: «Denaro reale attivo su N portafogli».
- **Pagina Broker:**
  - nuovi tipi «Alpaca — denaro reale» e «Trading 212 — denaro reale», con un bordo rosso;
  - procedura guidata:
    1. **Chiavi:** campi mascherati, con la guida sui permessi minimi e sulla restrizione IP.
    2. **Test in sola lettura** (solo `GET`), che mostra:
       - ID conto mascherato e tipo;
       - valuta e liquidità;
       - posizioni e ordini aperti, che devono essere zero;
       - cripto sì/no (Alpaca);
       - tipi d'ordine ammessi.
    3. **Prova a secco** (facoltativa, consigliata).
    4. **Attiva denaro reale:** finestra con le spunte obbligatorie, il nome del collegamento da digitare e il codice TOTP.
  - Pulsante rosso fisso **«Disattiva denaro reale»**, a un clic.
- **Testo dell'avviso di attivazione** (definitivo, salvo ritocchi):

  > **Stai per attivare il trading con denaro reale.**
  > Da questo momento gli agenti potranno inviare ordini **veri** al tuo conto {broker} ({conto}), **senza chiederti conferma per ogni ordine**, entro i limiti del portafoglio collegato: liquidità {liquidità} e profilo di rischio «{profilo}».
  > Puoi perdere in tutto o in parte il denaro investito. I risultati in simulazione o in paper non garantiscono risultati reali. Prezzi, commissioni e cambi reali possono differire dalle stime.
  > Riceverai una notifica Telegram per ogni ordine. Puoi fermare tutto in qualsiasi momento con «Disattiva denaro reale» o con il kill switch.
  > ☐ Ho letto e accetto i rischi · ☐ Il conto è dedicato a SMTrading · ☐ Ho impostato i limiti del profilo di rischio
  > Scrivi il nome del collegamento e inserisci il codice TOTP per confermare.

- **Ordini manuali dell'admin su un portafoglio reale:** finestra di conferma «Ordine con denaro reale» con il riepilogo. Basta un clic, senza TOTP.
- **TOTP obbligatorio:** se l'admin non ha attivato il TOTP, la procedura si ferma con «Attiva prima l'autenticazione a due fattori». Si riusa `_check_totp` con anti-replay.

### 10. Agenti via MCP
- `get_portfolio` e i metodi collegati mostrano già `mode`. Con il piano 8 diventa `reale`, con `live_state`.
- Gli strumenti, i permessi e il flusso degli ordini **non cambiano**. Un agente con `trade` su un portafoglio reale opera come in paper, dentro i limiti del profilo.
- Nuovi codici d'errore leggibili:
  - `LIVE_NOT_ARMED`, per un portafoglio reale sospeso;
  - `LIVE_DRY_RUN`, ordine non inviato in prova;
  - `LIVE_CASH_LIMIT`.
- Nessun metodo MCP può attivare, disattivare o collegare i conti reali.
- `docs/paperclip-mcp.md` riceve un paragrafo: «🔴 un portafoglio `reale` usa denaro vero».

### 11. Strategia di test (senza conti reali)
- **Fake con respx** per `api.alpaca.markets` e `live.trading212.com`, con le stesse fixture del paper/demo. La suite del nucleo di protocollo gira parametrizzata sulle 4 classi.
- **Rete chiusa nei test:**
  - `respx` con `assert_all_mocked` e `assert_all_called`;
  - un fixture di sessione fallisce se un socket verso un host live viene aperto davvero.
- **Guardie statiche:**
  - la scansione degli URL live (§1);
  - le classi paper rifiutano gli host live;
  - le classi live rifiutano qualsiasi altro host.
- **Cancello live:**
  - test per ogni stato (`configured`, `dry_run`, `armed`) × kill switch × locked;
  - con il cancello chiuso, il `POST` non arriva mai al transport fake.
- **Prova a secco end-to-end:** ordine dell'agente via MCP → rischio → BrokerSync → payload salvato → nessun `POST` → Telegram «🧪».
- **Scenari di protezione:**
  - riconciliazione con differenza → reduce-only + 🔴;
  - esito sconosciuto con Trading 212 → nessun reinvio + reduce-only;
  - conto cambiato;
  - liquidità insufficiente;
  - fusibile di frequenza;
  - disattivazione con ordini aperti.
- **Segreti:** chiavi «canarino» cercate in log, errori, audit e outbox.
- **E2E Playwright:**
  - procedura guidata contro un backend con i fake;
  - TOTP di test;
  - badge e banner;
  - disattivazione a un clic.
- **Primo collaudo reale** (lo fa l'utente, non la CI): vedi la checklist qui sotto.

---

## Cosa deve fare l'utente al momento dell'attivazione
1. **Aprire il conto.**
   - Alpaca: domanda live da non residente, con W-8BEN e KYC. Verificare che l'Italia venga accettata, e in quel caso se la cripto è disponibile.
   - Trading 212: conto Invest.
   - Meglio un conto **dedicato**, senza posizioni né ordini.
2. **Versare** l'importo desiderato dall'app del broker.
3. **Creare le chiavi API live**:
   - Trading 212: con i permessi minimi (dati del conto, metadati, portafoglio, ordini lettura ed esecuzione, storico; niente Pies - Write) e, se possibile, la **restrizione all'IP** del server;
   - Alpaca: le chiavi live, distinte da quelle paper.
4. In SMTrading:
   - attivare il **TOTP** dell'admin, se non è attivo;
   - verificare che **Telegram** sia attivo e inviare un messaggio di prova.
5. **Broker → nuovo collegamento «denaro reale»**: incollare le chiavi e lanciare il test in sola lettura. Controllare conto, valuta e liquidità mostrati.
6. Creare o scegliere un **portafoglio vuoto** con:
   - profilo di rischio con limite per ordine e perdita giornaliera massima;
   - leva 1, senza short;
   - valuta coerente con il conto;
   - deposito nel ledger pari al budget (≤ liquidità del broker).
7. Facoltativo ma consigliato: la **prova a secco** per qualche giorno, per controllare su Telegram cosa avrebbero fatto gli agenti.
8. **«Attiva denaro reale»**: leggere l'avviso, spuntare, scrivere il nome e inserire il TOTP. Poi **collegare il portafoglio** (TOTP).
9. **Primo collaudo** con un budget piccolo: un acquisto minimo a mercato in orario regolare. Verificare il messaggio Telegram, l'eseguito nel ledger, la posizione al broker e una riconciliazione «ok».
10. Ricordare i comandi di emergenza: «Disattiva denaro reale» (un clic), il kill switch e l'app del broker.

## Rischi aperti
- **Alpaca per residenti italiani non confermato:** l'elenco dei paesi non è pubblico. Se il conto viene rifiutato, si usa solo Trading 212. La cripto per i residenti UE con MiCA è dubbia.
- **API Trading 212 in beta:** le pagine della documentazione sono incoerenti sui tipi d'ordine in live. Gli scope e i limiti possono cambiare senza preavviso. I limiti di frequenza sono per conto, quindi gli script dell'utente li condividono.
- **Conto dedicato:** su Trading 212 un residente italiano ha di fatto un solo conto Invest. Se l'utente vuole usarlo anche a mano, la riconciliazione darebbe differenze continue. Alternativa da decidere: una «base esterna», cioè una fotografia delle posizioni preesistenti esclusa dal confronto, con un rischio maggiore di errori.
- **Valuta e FX:**
  - Trading 212 opera solo nella valuta principale del conto: il costo FX entra nei prezzi e nella liquidità e va riconciliato con una tolleranza;
  - Alpaca è in USD e richiede bonifici in USD, con costi bancari.
- **Trailing stop simulati:** dipendono dal worker. Se il worker è fermo, lo stop non scatta.
- **Dividendi e corporate action reali** non importati: causano differenze di liquidità e quindi reduce-only. Accettabile come comportamento prudente, ma fastidioso.
- **Autonomia degli agenti:** per decisione dell'utente non c'è approvazione per ordine. Le sole difese sono:
  - limiti del profilo;
  - tetto di liquidità;
  - fusibile di frequenza;
  - kill switch;
  - Telegram.

  Un permesso separato `trade_real` per assegnazione resta un'opzione non adottata.
- **Compromissione del server:** chi ottiene la master key e il database ha le chiavi live. Mitigazioni: restrizione IP su Trading 212, permessi minimi sulla chiave, conto dedicato con budget limitato. Di più non si può fare senza un HSM.
- **Fiscale:** regime dichiarativo a carico dell'utente (vedi la nota).

## Lasciato al builder
- I nomi esatti di colonne, tabelle e indici, e il numero della migrazione (la prima libera dopo il piano 7).
- La forma del nucleo di protocollo (mixin o composizione), purché gli URL restino `ClassVar` costanti nelle classi concrete.
- Il testo esatto dei messaggi Telegram e la tolleranza della riconciliazione della liquidità (proposta: max(0,50 €, 0,1%)).

## Dove si inserisce
Piano 8, dopo il piano 7 (serve l'outbox Telegram). Ramo nuovo impilato `feat/plan-08-real-trading`, con lo stesso processo di revisione. La chiusura del piano significa: **codice pronto, nessun collegamento live creato**. L'attivazione resta un'azione futura dell'utente.
