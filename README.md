# SMTrading

Simulatore di trading per agenti AI: backend FastAPI (API admin, MCP, worker), Postgres,
Redis e l'app web di amministrazione in italiano, tutto in `docker compose`.

## Avvio

1. Copia `.env.example` in `.env` e compila i valori. `SMT_ADMIN_PASSWORD` deve avere
   **almeno 10 caratteri**: con una password più corta l'admin non viene creato.
2. `docker compose up -d --build`
3. Dal PC stesso: <http://127.0.0.1:3000/> (ingresso locale, http).

L'API (`127.0.0.1:8000`, anche per l'MCP) resta raggiungibile solo dal PC.

## Utenti del portale

In *Impostazioni → Utenti* un amministratore crea gli altri utenti: **Amministratore**
(può fare tutto) o **Sola lettura** (vede tutto; può solo «Disattiva denaro reale» e
attivare il kill switch). La password temporanea è mostrata una sola volta e va cambiata
al primo accesso. Gli utenti non si cancellano: si disattivano. Recupero d'emergenza
dalla riga di comando (stesse protezioni, almeno un amministratore attivo):

```
docker compose exec -T api python -m smtrading.cli users list
docker compose exec -T api python -m smtrading.cli users create email@esempio.it --role admin
docker compose exec -T api python -m smtrading.cli users set-role email@esempio.it viewer
docker compose exec -T api python -m smtrading.cli users disable email@esempio.it
docker compose exec -T api python -m smtrading.cli users reset-password email@esempio.it
```

**Registro attività** (*Registro attività*, solo amministratori, esportabile in CSV): ogni
azione fatta dal portale (accessi, ordini manuali, kill switch, blocchi, chiusure di borsa,
collegamenti, impostazioni…) con utente, ruolo, esito, IP e un riassunto senza segreti.

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

Di base sono attivi i provider gratuiti (Yahoo, Kraken, Binance, CoinGecko, Frankfurter e
`rssnews`, le notizie crypto dai feed RSS pubblici di CoinDesk e Cointelegraph, prime nella
priorità delle news crypto; nessuna chiave).
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
Trading 212 Demo, eToro Demo: vedi *eToro* qui sotto). Gli adattatori paper/demo accettano solo gli indirizzi paper/demo e
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
  le nostre e registra le differenze; non corregge nulla. Il controllo periodico dei
  collegamenti di prova (ogni 5 minuti) registra le differenze nel feed delle attività e
  aggiorna lo stato del collegamento, ma non manda più avvisi Telegram: gli avvisi al
  proprietario sulle differenze col broker arrivano solo dal controllo della pagina
  **Riconciliazione** qui sotto.
- Pagina **Riconciliazione** (Sistema): per ogni portafoglio collegato (prova o denaro
  reale) posizioni, liquidità e ordini aperti di SMTrading contro il conto del broker, con
  stato, voci, storico e *Riconcilia ora* (amministratori). Il worker la ripete ogni 15
  minuti durante le sedute, una volta dopo la chiusura USA e, il sabato e la domenica,
  ogni 2 ore se un portafoglio collegato ha posizioni crypto; sempre solo in lettura
  (nessun ordine inviato o annullato). Una differenza che resta per 2 controlli di fila manda un
  avviso Telegram (categoria sistema, una volta al giorno per voce) e una voce nel feed
  degli agenti del portafoglio: decide il Risk Manager, nulla si corregge da sé. Un broker
  che non risponde dà «non verificabile», senza avvisi.

- **Quantità frazionarie su Alpaca** (paper e reale): Alpaca accetta un ordine frazionario
  solo «per la giornata». Uno stop, un target o un limit `gtc` frazionario viene inviato
  come ordine della giornata e, quando Alpaca lo fa scadere alla chiusura senza
  esecuzioni, BrokerSync lo rinvia all'apertura successiva con un nuovo riferimento (mai due
  volte; l'OCO resta valido: se l'altra gamba si esegue, il rinnovo è annullato). Il
  rinnovo è annotato nel registro attività (`broker.order_renewed`). Se scade dopo
  un'esecuzione parziale non viene rinviato: notifica `broker.renewal_failed` e l'allarme
  di protezione segnala la parte scoperta. L'allarme considera protetto uno stop in attesa
  di rinvio finché il mercato è chiuso e nei primi 15 minuti di seduta. Su un conto reale
  con il kill switch attivo il cancello live blocca anche il rinnovo (l'ordine è rifiutato
  e l'allarme lo segnala).

#### Prova collegamento (solo paper/demo)

Sulla scheda di un collegamento paper/demo, *Prova collegamento* (solo amministratori)
esegue in ordine: accesso (chiavi), lettura del conto, potere d'acquisto, stato del
mercato e una quotazione dello strumento di esempio (AAPL di default), con l'esito di ogni
passo in italiano. Facoltativo, con una seconda conferma scritta («ORDINE DI PROVA»): un
acquisto limite di 1 unità a metà prezzo (non può essere eseguito) inviato al conto di
prova e annullato subito, poi riletto. Non è disponibile dove il broker accetta solo ordini
a mercato (eToro) né con il kill switch attivo. L'ordine di prova esiste solo presso il
broker (nessun ordine o movimento in SMTrading). Rifiutata sui collegamenti a denaro reale,
che hanno il loro test in sola lettura. Ogni prova è nel *Registro attività*
(«Prova guidata del collegamento broker», con o senza ordine di prova).

#### Avviare una campagna di prova su un broker (paper/demo)

1. **Conto di prova**: apri o azzera il conto paper/demo presso il broker (Alpaca Paper,
   Trading 212 Demo, eToro Demo: per eToro un conto virtuale vuoto e la chiave *Demo*).
   Annota il saldo iniziale.
2. **Collegamento**: *Broker → Nuovo collegamento*, tipo paper/demo, chiavi del conto.
3. **Prova collegamento**: lancia la prova guidata; tutti i passi devono essere *OK*
   (una quotazione mancante va sistemata nei *Connettori*). Se il broker accetta ordini
   limite, ripetila con l'ordine di prova e controlla nell'app del broker che l'ordine
   risulti annullato.
4. **Portafoglio dedicato**: crea un portafoglio nuovo (o azzerane uno senza posizioni) con
   liquidità iniziale non superiore al saldo del conto di prova, poi collegalo al broker.
   Accetta le **commissioni del broker** proposte (o applicale da *Commissioni*): ogni
   preset riporta fonte e data della verifica; quelle marcate «da verificare» vanno
   controllate sul listino del proprio paese.
5. **Primo ordine piccolo**: un ordine manuale di poche unità (o frazionario su Alpaca) e
   la verifica che stato, esecuzione e commissione coincidano con l'app del broker.
6. **Riconciliazione**: *Riconcilia ora* dopo il primo fill; nessuna differenza attesa.
7. **Agenti**: assegna il portafoglio agli agenti come un qualsiasi portafoglio; segui i
   primi giorni da *Attività* (rifiuti, rinnovi giornalieri, riconciliazioni) e
   dall'allarme di protezione.
8. **Chiusura della campagna**: chiudi le posizioni, annulla gli ordini, scollega il
   portafoglio e confronta il risultato con il conto di prova.

### eToro (demo e reale)

eToro si collega in due modi: **eToro Demo** (`etoro_demo`, denaro virtuale, pagina
*Broker → Nuovo collegamento*) ed **eToro reale** (`etoro_live`, nella sezione *Denaro
reale*, con tutte le protezioni del denaro reale). Regole comuni:

- **Conto dedicato:** il conto (virtuale o reale) deve essere vuoto, senza posizioni,
  ordini o copy trading, quando si collega il primo portafoglio; con posizioni o ordini
  aperti il collegamento è rifiutato. Per il demo: chiudi tutto o azzera il portafoglio
  virtuale dall'app eToro.
- **Solo leva 1 e solo acquisti:** un acquisto apre una posizione eToro senza leva
  (`real`, lato buy); una vendita chiude le posizioni di quello strumento dalla più vecchia
  (FIFO), anche in parte. Niente short e niente leva, né in demo né in reale.
- **Solo ordini a mercato al broker:** eToro riceve soltanto ordini a mercato. Ingressi
  limit, stop, stop-limit, trailing stop, take profit, stop loss dei bracket e coppie OCO
  (PROTEGGI) sono accettati e **seguiti da SMTrading**: restano in SMTrading (non al
  broker) e, quando la quotazione raggiunge il livello (stesse quotazioni e stessi
  controlli del simulatore: quotazioni sospette o vecchie ignorate, solo a mercato aperto,
  scadenze rispettate), partono come ordine a mercato per la loro quantità, al prezzo
  di eseguito di eToro. Nell'OCO l'altro ordine si annulla appena uno parte; con il kill
  switch partono solo le uscite. Per gli agenti l'ordine resta quello inserito (stesso
  tipo e prezzi). La riconciliazione non li cerca al broker e l'allarme di protezione
  conta gli stop così seguiti. Una vendita oltre la posizione (short) resta rifiutata.
  Strumenti: azioni USA ed europee,
  ETF e cripto in USD (`BTC-USD` → `BTC`); uno strumento che eToro non vende senza leva è
  rifiutato; la mappatura del simbolo si cambia per strumento nella pagina *Mercati*.
- **Chiavi:** sul portale API di eToro
  (<https://api-portal.etoro.com/core/getting-started/authentication.md>) si crea una chiave
  utente per **un solo ambiente**: *Demo* con permesso *Write* per il collegamento demo,
  *Real* con permesso *Write* per quello reale. Nel collegamento si incollano la chiave API
  (`x-api-key`) e la chiave utente (`x-user-key`); non riusare la stessa chiave in due
  collegamenti né nel connettore dati (sotto).
- **Commissioni:** su ogni esecuzione si registra la commissione comunicata da eToro. La
  commissione di cambio (conversioni da e verso USD, secondo il listino eToro) si imposta
  nelle regole di commissione (*Commissioni*, colonna «Cambio %»): entra nelle anteprime e
  nel tetto di liquidità del denaro reale.

**Da verificare con la chiave reale dell'utente prima di attivare il denaro reale:** tre
risposte dell'API eToro sono state scritte dalla documentazione e provate solo contro il
broker finto. Il codice le legge in modo prudente (una forma sconosciuta non inventa mai
prezzi o quantità: l'ordine resta in attesa), ma vanno controllate con un test reale
prima di «Attiva denaro reale»:

1. la risposta di idoneità degli strumenti (leva 1, `real`, chiusura parziale ammessa);
2. l'involucro della ricerca di un ordine per riferimento;
3. i campi dell'esito di una chiusura: stato, unità chiuse, prezzo di chiusura.

### eToro come fonte di dati (pagina *Connettori*)

Il connettore `etoro` fornisce quotazioni e barre a **tutti** i portafogli (simulati, demo o
reali, su qualsiasi broker), come Yahoo o Kraken, solo per **azioni USA e cripto in USD**.

- **Chiave dedicata:** una chiave utente *Demo* con permesso *Read* (non tocca denaro). Se
  «Verifica» risponde con un errore di autorizzazione anche con una chiave Demo valida, usa
  una chiave *Real* con permesso *Read*: il connettore legge solo i dati di mercato e non
  può né leggere il conto né negoziare. Non riusare le chiavi dei collegamenti eToro (i
  limiti di frequenza valgono per chiave utente: dati e ordini si farebbero concorrenza).
- **Nasce spento e in fondo alle priorità** di azioni USA e cripto: finché l'admin non lo
  attiva, inserisce le chiavi, preme «Verifica» e, se vuole, lo sposta più in alto nelle
  priorità, nulla cambia. A quota esaurita il router passa subito alla fonte successiva.

### Chiusure di borsa (pagina *Mercati*)

Oltre al calendario ufficiale di ogni borsa, l'admin può segnare a mano un **giorno chiuso**
o una **chiusura anticipata** (con l'orario) di un mercato, con una nota: hanno la
precedenza sul calendario, valgono subito per gli orari di `get_market_status`, la
validità «giornata» degli ordini, la coda degli ordini a mercato e i controlli del denaro
reale («un giorno di borsa» si conta dal calendario del mercato, festività comprese). Ogni
aggiunta o rimozione va nell'audit. Londra (`LSE`) c'è solo per orari e festività: i titoli
londinesi non sono negoziabili.

### Ordini manuali (dettaglio portafoglio → *Ordini* → «Nuovo ordine»)

L'admin può inserire un ordine a mano: simbolo (dal catalogo), lato, tipo (a mercato,
limite, stop, stop limite, trailing stop), quantità o controvalore (solo a mercato), prezzi
e durata (GTC o giornata). «Invia» si abilita solo dopo un'**Anteprima** riuscita degli
stessi dati (gli stessi controlli di `preview_order`). L'ordine passa da tutti i controlli
degli ordini degli agenti: limiti di rischio, kill switch e blocco aperture (passano solo
le chiusure); ha origine `admin` ed è registrato nell'audit. Su un portafoglio reale (anche
in prova a secco) «Invia» apre **«🔴 Ordine con denaro reale»** con riepilogo, importo
stimato e **TOTP**. Un ordine aperto si annulla con «Annulla» nella riga, senza TOTP.

### Etichette delle modalità (solo portale)

Il portale nomina la modalità con il broker: «SMTrading (simulato)» senza broker,
«Alpaca · prova», «Trading 212 · demo», «eToro · demo» per i conti di prova e
«🔴 Alpaca · reale», «🔴 Trading 212 · reale», «🔴 eToro · reale» per il denaro reale, con
«· prova a secco» o «· sospeso» secondo lo stato del collegamento. La Dashboard conta i
portafogli per etichetta. **Gli agenti non vedono nulla di tutto questo**: le risposte MCP
non cambiano e con `mcp_hide_mode` (attivo di default) non contengono parole da simulazione.

### Pulizia dei nomi

Gli agenti vedono i nomi di portafogli e agenti: un nome con parole come `test`, `demo`,
`paper`, `sim`, `prova`, `fake`, `smoke` rivelerebbe la simulazione. Al salvataggio il
portale avvisa («gli agenti vedranno questo nome»), senza bloccare. Per i nomi esistenti:

```
docker compose exec -T api python -m smtrading.cli names audit        # elenca, propone nomi neutri
docker compose exec -T api python -m smtrading.cli names fix --yes    # applica le proposte
```

`fix` va eseguito solo dopo aver approvato i nomi proposti da `audit`.

### Denaro reale (pagina *Broker*, sezione «Denaro reale»)

> **Stato: costruito, non attivato.** Il codice per operare con denaro vero su Alpaca
> (live), Trading 212 (Invest) ed eToro (reale) è pronto e testato solo contro un broker
> finto; **non esiste nessun collegamento live** e nessuna chiave reale è salvata.
> L'attivazione è un'azione futura dell'utente, con la checklist qui sotto. Per eToro
> vanno prima verificate le tre risposte elencate in *eToro*.

Un portafoglio collegato a un conto reale mostra il badge rosso con il broker (per esempio
**🔴 Trading 212 · reale**, «· prova a secco» durante la prova, «· sospeso» quando il
collegamento è disattivato) e, finché un collegamento è attivo, l'intestazione di ogni pagina
mostra il pallino rosso «Reale» (il suggerimento dice «Denaro reale attivo su N portafogli» e quali;
un clic porta a «Disattiva»). Gli ordini reali sono **autonomi** (nessuna approvazione per ordine): le
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
   l'Italia sia accettata ed eventualmente la cripto). Trading 212: conto Invest. eToro: un
   conto reale senza copy trading. Serve un conto **dedicato** a SMTrading, senza posizioni
   né ordini aperti (test, attivazione e collegamento del primo portafoglio rifiutano un
   conto non vuoto); per Alpaca un conto *cash* (niente margine), per eToro solo posizioni
   senza leva.
2. **Versare** l'importo desiderato dall'app del broker.
3. **Creare le chiavi API live.** Trading 212: permessi minimi (dati del conto, metadati,
   portafoglio, ordini lettura ed esecuzione, storico; **niente «Pies - Write»**) e, se
   possibile, la restrizione all'IP pubblico del server. Alpaca: le chiavi live, distinte
   da quelle paper. eToro: chiave utente dell'ambiente *Real* con permesso *Write*, distinta
   da quella demo e da quella del connettore dati.
4. In SMTrading: attivare il **TOTP** dell'admin (*Impostazioni*) e verificare che
   **Telegram** sia attivo, inviando un messaggio di prova (l'attivazione richiede una prova
   riuscita nelle ultime 24 ore).
5. **Broker → Denaro reale → Nuovo collegamento reale**: scegliere il tipo (`alpaca_live`,
   `t212_live` o `etoro_live`), incollare le chiavi e lanciare il **test in sola lettura**
   (valido 15 minuti). Controllare conto (mascherato), valuta, liquidità, zero posizioni e
   zero ordini aperti.
6. Creare o scegliere un **portafoglio vuoto** con: profilo di rischio con limite per
   ordine e perdita giornaliera massima, short non consentito; leva 1; deposito nel ledger
   pari al budget e non superiore alla liquidità del broker. La valuta può essere diversa
   da quella del conto (vedi *Più valute nel reale*).
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
  `docker compose exec -T api python -m smtrading.cli live disarm "<nome collegamento>" --reason "<motivo>"`
  (nessun TOTP); `docker compose exec -T api python -m smtrading.cli live status` elenca i collegamenti.
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

#### Più portafogli reali su un conto

Un collegamento reale può avere più portafogli, ognuno con la sua **quota**: il suo
contante convertito nella valuta del conto (al cambio peggiore tra corrente e ultimo noto,
più l'1%). La scheda del collegamento elenca i portafogli con la quota, il contante al
broker, quanto è assegnato e quanto resta **non assegnato** (fermo, non usato da nessuno).

- Collegare un portafoglio richiede sempre il TOTP ed è ammesso solo se la somma delle
  quote, con quella nuova, sta nel contante disponibile al broker. Il primo portafoglio
  richiede il conto vuoto; i successivi un test in sola lettura recente e l'ultima
  riconciliazione in regola per quelli già collegati. Si scollega solo senza posizioni.
- Un acquisto deve stare nel contante del suo portafoglio e in quello del broker meno le
  quote degli altri; una vendita solo fino alle posizioni del proprio portafoglio.
- Riconciliazione per simbolo: una differenza su un simbolo mette in **sola chiusura solo
  i portafogli che hanno quel simbolo** (tutti, se nessuno lo ha); contante del broker sotto
  la somma delle quote mette in sola chiusura **tutti**.
- **Valgono per tutto il collegamento**, quindi per tutti i suoi portafogli: disattivazione,
  prova a secco, kill switch, fusibile degli ordini all'ora (conta gli invii di tutti i
  portafogli, con il limite più basso dei loro profili) e cambio di conto.
- Con eToro le posizioni al broker sono del conto: una vendita può chiudere posizioni
  aperte da un altro portafoglio; il registro di SMTrading resta la verità per ciascuno.

#### Più valute nel reale

La valuta del portafoglio può essere diversa da quella del conto (per esempio un
portafoglio in EUR su un conto eToro in USD). Contante, limiti e quote si convertono al
cambio corrente, con un margine dell'1%; la riconciliazione confronta nella valuta del
broker. Una differenza di contante spiegata **solo dal movimento del cambio** (posizioni
coincidenti) dà **solo un avviso** «scarto di cambio» su Telegram e nel portale, al massimo
uno al giorno per collegamento, senza sola chiusura. Senza un cambio disponibile non
partono nuovi acquisti in valuta diversa (l'ordine attende e scade dopo 30 minuti); le
vendite restano ammesse. Le commissioni di cambio dei broker si impostano nelle regole di
commissione («Cambio %»).

#### Limiti noti

- **Prezzo di riserva del tetto di liquidità:** senza quotazione si usa l'ultimo eseguito
  del portafoglio sullo stesso simbolo (×1,02) se è successivo all'apertura della seduta
  precedente del mercato dello strumento («un giorno di borsa», dal calendario: festività
  e chiusure manuali saltate); altrimenti l'ordine è rifiutato finché non c'è una
  quotazione.
- **Broker finto solo per i test:** l'impostazione `SMT_LIVE_FAKE_BROKER` (spenta di
  default) sostituisce i broker reali con un broker in memoria; la usa solo lo stack e2e
  (`scripts/e2e.sh`). Quando è accesa, API e worker scrivono all'avvio un avviso nei log:
  non va mai attivata nello stack principale. Il seed e2e
  (`python -m smtrading.devtools.live_e2e_seed`) si rifiuta di partire se è spenta.
- Trailing stop simulati (dipendono dal worker), dividendi e corporate action reali non
  importati (differenze di liquidità → reduce-only), API Trading 212 in beta, tre risposte
  eToro da verificare (vedi *eToro*): vedi
  «Rischi aperti» nello spec `docs/superpowers/specs/2026-11-14-real-trading-design.md`.

**Nota fiscale:** con Alpaca, Trading 212 ed eToro si applica di norma il regime dichiarativo (quadri
RT, RM, RW): SMTrading conserva lo storico ma non calcola imposte. Vedi la «Nota fiscale»
dello spec `docs/superpowers/specs/2026-11-14-real-trading-design.md` e verificare con un
commercialista.

### Watchlist, alert e backtest

Disponibili per gli agenti via MCP (vedi `docs/paperclip-mcp.md`): watchlist (quotazioni
seguite in streaming), alert di prezzo e di variazione percentuale (arrivano come notifiche
`price_alert`), backtest di strategie solo long (`sma_cross`, `rsi_reversion`, `breakout`,
`buy_and_hold`) su barre storiche, senza toccare portafogli né broker.

### Catalogo strumenti (pagina *Mercati*)

Il catalogo parte con circa 900 strumenti (USA: S&P 500, Nasdaq-100 ed ETF; Europa `.MI/.DE/.PA`:
FTSE MIB, DAX, CAC 40, principali MDAX ed ETF UCITS; circa 65 crypto in USD; forex).
La colonna *Seguito da* mostra quali agenti hanno lo strumento in una watchlist. Con
l'**aggiunta automatica** attiva (interruttore in *Mercati*) un agente che usa un simbolo
non in catalogo lo aggiunge se supera i controlli (massimo 25 aggiunte al giorno per
agente); spenta, un simbolo sconosciuto è `SYMBOL_NOT_FOUND`. L'origine di ogni strumento
è visibile in tabella.

Ogni strumento ha **settore** (11 settori in italiano, oppure ETF / Crypto / Cambi),
**industria** (per gli ETF il tipo: azionario, obbligazionario, materie prime, leva e
inverso, monetario) e **indici** di appartenenza, letti una volta dai provider e scritti nei
CSV del catalogo (`scripts/enrich_catalog.py`): il seed riempie solo i valori mancanti, le
modifiche dell'admin restano. La pagina offre la **ricerca istantanea** (simbolo, nome,
settore, industria, indice; frecce e Invio aprono la scheda), la **scheda strumento**
(prezzo e variazione, grafico giornaliero / orario, RSI, medie 20/50, ATR, prossimi
risultati e dividendi, nostre posizioni e ordini, strategie e segnali, watchlist degli
agenti, notizie), i **filtri** per classe, mercato, settore, indice e i filtri rapidi (in
portafoglio, nelle watchlist, usati da strategie, con segnale attivo, aggiunti di recente),
la **mappa dei settori** e i **titoli più mossi** di un universo (S&P 500, NASDAQ-100, FTSE
MIB, DAX, CAC, MDAX, crypto, watchlist e posizioni), calcolati dalla chiusura precedente con
richieste a blocchi (Alpaca, Yahoo) solo a pagina aperta, in cache 5 minuti. Filtri,
ordinamento e universo restano memorizzati nel browser. Solo informativo: la mappa non
entra mai nelle decisioni di trading.

### Campagne e resoconto (pagina *Campagne*)

Una **campagna** è una prova con un nome e un periodo che raggruppa dei portafogli (stato
*bozza*, *attiva* o *conclusa*). *Nuova campagna* (solo admin) guida in quattro passi:

1. **Nome e date** (la fine è anche la fine dei piani della campagna) e note;
2. **Portafogli**: nuovi (nome, capitale, valuta, profilo di rischio) o esistenti, ognuno
   facoltativamente su un conto **paper/demo** già creato in *Broker* (con «Prova
   collegamento»); i collegamenti a denaro reale non sono mai proposti e sono rifiutati,
   come i portafogli già collegati a denaro reale. Un portafoglio sta in una sola campagna
   aperta;
3. **Obiettivi** dai modelli: per ogni portafoglio quelli della campagna (minimo, perdita
   massima, battere un indice, dal giorno di inizio a quello di fine) e i modelli della
   pagina *Obiettivi*; le regole sono quelle degli obiettivi (al massimo un piano per
   portafoglio; con il piano settimanale sul totale attivo nessun obiettivo di portafoglio
   può essere un piano);
4. **Riepilogo** e conferma («Crea e avvia» o «Salva come bozza»): tutto viene creato in
   un'unica operazione, se qualcosa non va non resta nulla a metà.

*Chiudi campagna* mostra posizioni e ordini aperti, lo stato della chiusura dei piani e
quali portafogli si possono archiviare (le regole dell'archivio valgono: con ordini aperti
o un broker collegato non si archivia), poi conclude la campagna e offre il **resoconto
finale**. Le campagne non sono visibili agli agenti: la fine della campagna arriva loro
dagli obiettivi.

Il **Resoconto** (PDF, anche per chi ha la sola lettura) si scarica da ogni campagna o dal
pulsante *Resoconto* di *Dashboard*, *Portafogli* e *Campagne* per qualsiasi periodo e
portafogli: rendimento per portafoglio e totale con il confronto con SPY, QQQ e VWCE (dai
campioni degli obiettivi «battere un indice»: senza obiettivi di quel tipo «dati non
disponibili»), grafico dell'andamento, operazioni con P&L realizzato e costi, risultati per
strategia, esito degli obiettivi, costi degli agenti (di tutto il team), lezioni, regole
del team e decisioni rilevanti del periodo. Depositi e prelievi non sono neutralizzati.

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
| 🟠 avviso | ordine rifiutato, kill switch disattivato, avviso di margine, liquidazione in attesa del broker, prezzo di esecuzione mancante, modifica ordine non riuscita, differenze di riconciliazione broker trovate con *Riconcilia ora* (quelle del controllo periodico arrivano dal controllo di riconciliazione, vedi sopra) |
| 🟢 eseguito | ordine eseguito |

Il resto (ordini accettati o annullati, alert di prezzo, decisioni) compare solo nel feed
delle attività e nei riepiloghi.

**Messaggi Telegram in più** (*Notifiche → Messaggi Telegram*):

- **Controllo pre-apertura** (attivo di default alle 08:45, giorni feriali): un solo
  messaggio con i mercati del giorno e lo stato del sistema. «✅ Pronti per l'apertura» se
  va tutto bene, altrimenti l'elenco dei problemi (kill switch o blocchi attivi,
  sincronizzazione broker o monitor del denaro reale fermi, collegamenti in errore o con
  dati del conto vecchi, quotazioni mancanti o vecchie, portafogli in sola riduzione,
  isolamento Paperclip non verificato, Redis o database non raggiungibili). Nei giorni con
  le borse chiuse dice solo che sono chiuse.
- **Obiettivi** (pagina «Obiettivi»; di default l'obiettivo settimanale +2% sul totale):
  il riepilogo giornaliero e quello settimanale mostrano una riga «🎯» per ogni obiettivo
  attivo (settimana, mese, trimestre, anno o date fissate) con l'avanzamento del periodo, e
  il giorno dopo la fine di un periodo una riga «🏁» con l'esito.
- **Resoconto settimanale in PDF** (attivo di default, il venerdì alle 22:30, dopo la
  chiusura USA e il giro del PM delle 22:15; giorno e orario si cambiano): il *Resoconto*
  della settimana, da lunedì alle 00:00 a quel momento, inviato nel gruppo come documento
  PDF con una breve didascalia (settimana, P&L totale in € e %, obiettivo settimanale
  raggiunto sì/no, costo degli agenti). Il resoconto somma solo i portafogli conteggiati
  nei totali; quelli fuori dai totali sono in una pagina a parte, «Portafogli fuori dai
  totali». Uno solo per settimana, anche dopo un riavvio; se il worker era fermo o
  Telegram non era pronto parte entro 24 ore dall'orario, poi la settimana è saltata (un
  WARNING nel log). Un PDF oltre i 20 MB non si invia: arriva la sola didascalia, con
  l'invito a scaricarlo dal pulsante *Resoconto*. Il file resta nella coda solo finché
  non è inviato.
- **«🧭 Decisioni di oggi»** nel riepilogo delle 18:00 (e «della settimana» nel
  settimanale): le decisioni registrate dagli agenti con `log_decision`, per tipo, una riga
  ciascuna con esito, oggetto, portafoglio e motivo (campi `outcome`, `subject`, `reason`).

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

#### Paperclip: costi e isolamento

SMTrading **legge** Paperclip (solo richieste GET, ogni 5 minuti nel worker) per due cose.
Si configura in *Impostazioni → Paperclip*: indirizzo, id azienda, chiave board (cifrata,
mai mostrata né scritta nei log), interruttore «Lettura attiva»; «Verifica» fa una lettura
di prova ed elenca gli agenti Paperclip con l'abbinamento agli agenti SMTrading (per nome,
modificabile).

- **Costi** (pagina *Costi agenti*): costo per agente e per giorno, settimana e mese, in
  **dollari** come in Paperclip, distinguendo «a consumo API» da «incluso
  nell'abbonamento», accanto a ordini e rendimento dei portafogli dell'agente.
- **Risultati** (pagina *Risultati agenti*, sola lettura, mai visibile agli agenti): per
  settimana in corso, 1 mese, 3 mesi o tutto, il riepilogo del team (risultato netto delle
  operazioni chiuse, ordini eseguiti, veti, lezioni, regole attive, costo agenti e
  risultato per 1 $ di costo), per agente decisioni per tipo, proposte approvate e
  respinte, lezioni scritte e che lo riguardano (anche ripetute), chiamate MCP ed errori,
  ordini eseguiti e costo, per strategia operazioni, vincenti, risultato netto, durata
  media e posizioni aperte.
- **Isolamento:** le regole di isolamento degli strumenti di Paperclip sono confrontate con
  quelle attese, fotografate al primo avvio e riconfermate dall'admin con un pulsante
  (dopo aver controllato le modifiche in Paperclip). Regole rimosse, disattivate o
  modificate danno un avviso critico su Telegram e un banner nel portale.
- Paperclip spento o chiave errata: lo stato è nella sezione e c'è un solo avviso dopo 3
  errori consecutivi, niente allarmi a raffica.

### Backup e ripristino

Il servizio `backup` del compose (stessa immagine del backend, `python -m
smtrading.backup.main`) lavora da solo finché Docker è acceso:

- **Ogni notte alle 03:30** (ora di `SMT_TZ`, Europe/Rome): `pg_dump -Fc` del database
  `smtrading` in `smtrading-AAAAMMGG-HHMM.dump` con l'impronta `…dump.sha256` (formato
  `sha256sum`), più `smtrading-AAAAMMGG-HHMM-config.tar.gz` (con la sua `.sha256`): le
  istruzioni degli agenti esportate per Paperclip (`agent-instructions/`), il
  `docker-compose.yml`, il `.env.example` e un `RESTORE.txt`. **Mai** `.env` né chiavi.
- **Destinazioni:** ogni cartella di `BACKUP_DIRS` (percorsi separati da `;`, dentro il
  container), create se mancano. Il compose monta
  `SMT_BACKUP_LOCAL_DIR` (predefinita `/mnt/d/Backups/SMTrading`, cioè
  `D:\Backups\SMTrading`) su `/backups/local` e `SMT_BACKUP_ONEDRIVE_DIR` (predefinita
  `/mnt/d/onedrive/OneDrive - SantoMagno/Founders - Founders/Backups/SMTrading`, la
  cartella sincronizzata con OneDrive) su `/backups/onedrive`. Per cambiarle basta
  impostarle in `.env` (gli spazi vanno bene, senza virgolette) e `docker compose up -d
  backup`. Il file viene scritto come `.partial` e rinominato solo quando è completo.
- **Conservazione**, in ogni cartella: l'ultimo backup di ciascuno degli ultimi 30 giorni
  con un backup e il primo di ciascuno degli ultimi 12 mesi (normalmente quello del 1°).
  Vengono cancellati solo file con questi nomi.
- **Prova di ripristino ogni domenica alle 04:00:** l'ultimo dump (con l'impronta
  verificata) viene ripristinato nel database usa e getta `smtrading_restore_test` sullo
  stesso server; deve essere alla versione di schema attuale (alembic head) e contenere
  portafogli, ordini ed esecuzioni; poi il database viene eliminato.
- Un orario saltato (PC spento) viene recuperato al successivo avvio del servizio.
- **Esito** (riuscito / parziale / non riuscito, dimensione, durata, cartelle) in
  «Salute del sistema», sezione *Backup*: **problema** se l'ultimo backup riuscito in tutte
  le cartelle ha più di 26 ore, **attenzione** se l'ultima esecuzione non è riuscita o se
  la prova di ripristino è fallita o ha più di 8 giorni. Un backup o una prova non riusciti
  mandano un messaggio Telegram (categoria sistema).
- **API:** `GET /api/backups` (file di ogni cartella, ultime esecuzioni, orari),
  `POST /api/backups/run` (solo admin; `{"restore_test": true}` aggiunge la prova di
  ripristino): il servizio la esegue entro 15 secondi. *Impostazioni → Scarica backup
  ora* resta per un dump immediato scaricato dal browser.

**La chiave di cifratura non è nei backup.** Le credenziali di connettori e broker, il
token del bot Telegram, la chiave board di Paperclip e i segreti TOTP (2FA) degli utenti
sono nel database cifrati con `SMT_MASTER_KEY` (in `.env`): senza quella chiave un
ripristino funziona ma le credenziali vanno reinserite e la 2FA azzerata
(`users reset-totp`). Il proprietario conserva `SMT_MASTER_KEY` (e
le altre voci di `.env`: `POSTGRES_PASSWORD`, admin, Telegram) **a parte**, per esempio in
un gestore di password, mai nella cartella dei backup né in OneDrive in chiaro.

Ripristino (dal PC, nella cartella del progetto; esempio con il backup del 20/11/2026):

```bash
B="/mnt/d/Backups/SMTrading"; F=smtrading-20261120-0330
cd "$B" && sha256sum -c "$F.dump.sha256" && cd -          # impronta ok
docker compose stop api worker backup                       # nessuno scrive nel frattempo
docker compose cp "$B/$F.dump" postgres:/tmp/restore.dump
docker compose exec postgres pg_restore -U smtrading -d smtrading \
  --clean --if-exists --no-owner --exit-on-error /tmp/restore.dump
docker compose exec postgres rm /tmp/restore.dump
docker compose up -d                                        # migrate porta lo schema all'head
```

Su un PC nuovo: clonare il progetto, ricreare `.env` da `.env.example` con la
`SMT_MASTER_KEY` conservata, `docker compose up -d postgres`, poi gli stessi comandi
(`pg_restore` su un database vuoto: `--clean --if-exists` non serve). Le istruzioni degli
agenti sono anche nel database; quelle esportate stanno in `…-config.tar.gz`
(`tar -xzf "$B/$F-config.tar.gz" -C /tmp/smt-restore`). Per provare un dump senza toccare
il database vero: `docker compose exec postgres createdb -U smtrading prova` e
`pg_restore -d prova`, poi `dropdb -U smtrading prova`.

I log dei processi (`docker compose logs`) non contengono chiavi: le richieste HTTP verso i
fornitori non sono registrate e un filtro maschera token, API key, password e credenziali
`Authorization` in ogni riga.

### Cambio della chiave master

`SMT_MASTER_KEY` cifra (AES-GCM, chiave = sha256 della `SMT_MASTER_KEY`) tutto e solo
questo: configurazioni dei connettori (`connectors.config_encrypted`), chiavi dei broker
paper/demo e reali (`broker_links.config_encrypted`), segreti TOTP degli utenti
(`admin_users.totp_secret_encrypted`), token del bot Telegram
(`app_settings.telegram.token_enc`) e chiave board di Paperclip
(`app_settings.paperclip.board_key_enc`). Non serve ad altro: le sessioni del portale sono
identificativi casuali in Redis (non firmati), il token CSRF è casuale, le password sono
hash argon2 senza «pepper», i token degli agenti non ne dipendono. **Dopo il cambio nessuno
viene disconnesso, le password restano valide e la 2FA continua a funzionare** (anche i
segreti TOTP vengono cifrati di nuovo).

Lo strumento `python -m smtrading.devtools.rotate_master_key`, eseguito nel container
`api`, legge la chiave attuale da `SMT_MASTER_KEY` e la nuova **solo** dalla variabile
`SMT_NEW_MASTER_KEY` (mai dalla riga di comando). In un'unica transazione, con le righe
bloccate, decifra ogni valore con la chiave attuale, lo cifra con la nuova, lo rilegge dal
database e lo verifica con la nuova chiave:

- `--dry-run` (predefinito): conteggi per campo, poi rollback: non scrive nulla;
- `--apply`: salva solo se **tutti** i valori superano la verifica, con una riga di audit
  `admin:master_key.rotate` (solo conteggi). Se anche un solo valore non si decifra con la
  chiave attuale non cambia nulla: elenca campo e id di ciascuno (esito `refused`
  nell'audit). Quella credenziale è già illeggibile per l'applicazione (risulta «da
  reinserire»): reinseriscila dal portale, oppure, per un TOTP, `users reset-totp`, e
  riprova;
- `--generate`: stampa una nuova chiave casuale di 64 caratteri, e nient'altro.

L'output non contiene mai chiavi, valori decifrati o cifrati: solo campi, conteggi e id.
La nuova chiave deve avere almeno 32 caratteri, almeno 12 diversi, due tipi di carattere,
niente spazi, ed essere diversa da quella attuale (codice di uscita 2 altrimenti).

Procedura (dal PC, nella cartella del progetto):

1. **Backup prima di tutto:**
   `docker compose exec -T postgres pg_dump -U smtrading -Fc smtrading > "/mnt/d/Backups/SMTrading/prima-cambio-chiave-$(date +%Y%m%d-%H%M).dump"`.
2. **Agenti fermi:** in Paperclip nessun agente al lavoro (come per un aggiornamento),
   nessuno usa il portale. Poi `docker compose stop worker backup` (l'`api` resta accesa
   per eseguire lo strumento).
   Nessun «Collega la TV» né «Manda alla TV» in corso (Schermo TV): l'`api` non deve
   salvare segreti mentre la chiave cambia, altrimenti quella TV va ricollegata.
3. **Genera la nuova chiave:**
   `docker compose exec api python -m smtrading.devtools.rotate_master_key --generate`.
4. **Salvala in KeePass** come nuova voce («SMT_MASTER_KEY dal GG/MM/AAAA»). La voce con la
   chiave attuale **non si cancella**: rinominala «SMT_MASTER_KEY fino al GG/MM/AAAA».
5. **Passala alla shell senza lasciarla nella cronologia:**
   `read -rs SMT_NEW_MASTER_KEY && export SMT_NEW_MASTER_KEY` (incolla, Invio: non si
   vede nulla).
6. **Prova:**
   `docker compose exec -e SMT_NEW_MASTER_KEY api python -m smtrading.devtools.rotate_master_key --dry-run`.
   Deve finire con `totale: N, verificati con la nuova chiave: N` e «prova (dry-run)».
7. **Applica:** lo stesso comando con `--apply` («applicato: … (commit)»). Da questo
   momento il database è cifrato con la nuova chiave: passa subito ai punti 8-9.
8. **Aggiorna `.env`:** sostituisci il valore di `SMT_MASTER_KEY` con la nuova chiave
   (`nano .env`, incolla), poi `unset SMT_NEW_MASTER_KEY`.
9. **Riavvia** ricreando i container (un semplice `restart` non rilegge `.env`):
   `docker compose up -d --force-recreate api worker backup`.
10. **Verifica:** *Connettori* (stato dei connettori con chiave), *Broker* («Prova
    collegamento» sui paper/demo, sezione «Denaro reale» senza chiavi illeggibili),
    *Notifiche* (messaggio di prova Telegram), «Salute del sistema» (Paperclip letto,
    backup), un accesso con 2FA da una finestra anonima; e
    `docker compose logs --since 10m api worker | grep -iE "cannot be decrypted|unreadable"`
    non deve mostrare nulla.
11. **La vecchia chiave resta in KeePass** finché la verifica non è completa **e** finché
    esistono backup fatti prima del cambio (fino a 12 mesi, vedi conservazione): un dump
    precedente si ripristina con la vecchia chiave in `.env`, poi si ripete il cambio.

Se dopo il riavvio qualcosa risulta illeggibile, controlla che in `.env` ci sia
esattamente la nuova chiave (e ricrea i container); in ultima istanza ripristina il dump
del punto 1 con la vecchia chiave in `.env`.

### Aggiornamenti

Dopo un `docker compose up -d --build` una scheda aperta con la versione precedente mostra
«Nuova versione disponibile — Ricarica». Per un identificativo esplicito della build:
`SMT_BUILD_ID=$(git rev-parse --short HEAD) SMT_BUILT_AT=$(date -Iseconds) docker compose up -d --build`
(altrimenti l'immagine web usa l'ora della build). Versione, ora della build e ultimo avvio di
api e worker si leggono in «Salute del sistema».

## Test

- Backend: `cd backend && uv run pytest -q && uv run ruff check .`
- Web: `cd web && npm test -- --run && npm run lint && npm run build`
- End-to-end: `bash scripts/e2e.sh` avvia uno stack separato (progetto
  `smtrading-e2e`, con volumi, rete e credenziali proprie, web su `127.0.0.1:3190`), esegue
  Playwright e lo elimina alla fine; lo stack principale non viene toccato. Solo quello
  stack gira con `SMT_LIVE_FAKE_BROKER=1`: dopo le altre spec lo script esegue il seed e2e
  del denaro reale e `web/e2e/live.spec.ts` (procedure Trading 212 ed eToro contro il
  broker finto, due portafogli su un conto eToro, chiavi «canarino», TOTP di test generato
  a ogni esecuzione).
- **Prova generale degli ordini:** `SMT_E2E_BATTERY=/percorso/batteria.jsonl bash
  scripts/e2e.sh` esegue anche `scripts/order_battery.py` nello stesso stack isolato: un
  agente di prova, via MCP con un token vero dello stack e2e, prova tutti i tipi d'ordine
  e le opzioni (market per quantità e importo, limit, stop, stop-limit, trailing, GTC e
  giornata, modifica, bracket, OCO, annullamenti), i rifiuti attesi, il conto reale eToro
  finto in prova a secco e attivo, gli ordini manuali dell'admin con TOTP e le verifiche
  trasversali (nessuna parola da simulazione per l'agente, outbox Telegram senza invii,
  riepilogo con le decisioni, registro attività, pre-apertura). Ogni prova scrive una riga
  JSON appena finisce. Non va mai lanciata contro lo stack principale (rifiuta di partire
  senza `SMT_LIVE_FAKE_BROKER=1`).
