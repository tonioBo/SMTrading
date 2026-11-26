# SMTrading — Lavoro in giornata in due modi: sveglia dell'agente e regole automatiche (scalping)

Data: 2026-11-26 · Stato: APPROVATO dall'utente il 2026-11-26 · Ramo: `feat/intraday` (worktree), in linea solo **spento**.
Unisce due modi di lavorare, **entrambi da provare** (Matteo, 26/11: «vorrei entrambe le possibilità
da testare»), ognuno con il suo interruttore per portafoglio:
- **Modo A — sveglia dell'agente** (minuti): esattamente come `2026-11-26-smtrading-intraday-design.md`
  (approvato), che resta valido per intero e qui non si ripete.
- **Modo B — regole automatiche** (secondi, scalping): questo documento.
Le fondamenta (posizioni di giornata, rete di fine seduta, PDT, storico a barre brevi, calendario,
guardia MCP) sono comuni ai due modi.

## Perché
Matteo (26/11/2026) vuole operatività **molto rapida, di secondi** (scalping). Con l'agente in mezzo a
ogni ordine passano 1-3 minuti e i costi dei cicli esplodono: allo scatto deve eseguire SMTrading da
solo. Decisione: **prima solo prove**, sui conti di prova (azioni USA e cripto), misurando costi,
ritardi e risultati veri; mercato e conto reale li decide Matteo dopo aver visto i numeri.

## Vincoli di mercato (da tenere presenti nei risultati)
- Azioni USA: con conto **margine sotto 25.000 $** max 3 operazioni di giornata ogni 5 sedute (PDT) →
  scalping impossibile; con conto **in contanti** (il collegamento reale Alpaca oggi è in contanti) il
  denaro di una vendita è disponibile solo dal giorno dopo. Scalping azioni = conto margine ≥ 25.000 $.
- Cripto (Alpaca): nessuna regola PDT, 24/7, ma commissione 0,15-0,25% a operazione: servono
  guadagni per operazione più ampi.
- eToro: lento e costoso, escluso.

## Decisioni dell'utente
- Allo scatto **esegue SMTrading** (in meno di un secondo); gli agenti in futuro sceglieranno e
  regoleranno le regole e ne controlleranno i risultati, non il singolo ordine (fuori da questo progetto).
- **Regole = modelli con parametri** (pochi tipi noti, numeri regolabili), le stesse sullo storico al
  minuto e dal vivo. Niente regole libere composte né codice degli agenti.
- **Solo prove** ora: tutto spento per i portafogli della campagna, invisibile agli agenti.
- **Prezzi:** prove con il flusso gratuito (IEX per le azioni, cripto Alpaca), misurando gli
  scostamenti; Algo Trader Plus (99 $/mese) solo all'accensione, con domanda a Matteo.
- **Conto della prova:** nuovo conto di prova Alpaca con collegamento proprio (chiavi da Luca/Matteo),
  saldo **30.000 $** (scelta di Claude: sopra i 25.000 $ lo scalping sulle azioni è possibile; i
  rifiuti PDT si provano con `max_day_trades_5d` del portafoglio).

## Obiettivi
1. **Motore di regole automatiche**: valuta i modelli attivi a ogni prezzo in arrivo ed esegue entrate
   e uscite da solo, con i controlli di rischio esistenti.
2. **Freni automatici** per regola e per portafoglio.
3. **Misura**: per ogni operazione tempi (scatto → invio → esecuzione), prezzo atteso vs eseguito,
   commissioni, risultato; pagina nel portale e riepilogo Telegram.
4. **Prova sullo storico** al minuto degli stessi modelli, con commissioni e forbice.
5. Modo A completo (progetto approvato) e fondamenta comuni: interruttori per portafoglio, posizioni di giornata, rete di fine seduta,
   regola PDT, storico a barre brevi, calendario, non regressione MCP.
6. Prova dal vivo sul nuovo conto di prova (azioni e cripto) e voce nel documento di prontezza.

## Non obiettivi
- Istruzioni e strumenti per gli agenti (scegliere/regolare regole): dopo, con domanda a Matteo.
- Vendite allo scoperto, opzioni, Europa/eToro, conto reale.
- Regole libere composte, codice scritto dagli agenti.

## Anti-obiettivi
- Gli agenti vedono qualcosa di nuovo durante la campagna; una messa in linea disturba la campagna.
- Un ordine automatico parte su prezzo vecchio, forbice larga, mercato chiuso o senza stop.
- Una regola continua a perdere senza fermarsi; il motore va in loop di ordini (doppio invio).
- Il motore rallenta il resto di SMTrading (flusso prezzi, stop, riconciliazione).

## Comune ai due modi (dal progetto del modo A)
- Campi sul portafoglio del modo A invariati (`intraday_enabled` = interruttore del modo A,
  `intraday_timeframes`, `intraday_wake_agent_id`, `intraday_max_wakes_per_hour`, `day_close_minutes`,
  `max_day_trades_5d`) più **`auto_rules_enabled`** (default false) = interruttore del modo B.
  I due interruttori sono indipendenti; `day_close_minutes` e `max_day_trades_5d` valgono per entrambi.
- `orders.day_trade`; posizioni di giornata; **rete di fine seduta** (azioni) solo a mercato aperto,
  limite all'ultimo scambio meno margine con forbice larga, `tif=day`, avviso Telegram.
- **PDT** per collegamento: le entrate automatiche e manuali `day_trade` sono rifiutate a budget
  finito; le vendite mai; avviso sugli altri portafogli dello stesso collegamento. Per provare i
  rifiuti su un conto ≥ 25.000 $ si usa `max_day_trades_5d` del portafoglio.
- Storico a barre brevi Alpaca, calendario di giornata, guardia MCP (impronta degli strumenti invariata).

## Approcci considerati (regole)
1. **Modelli con parametri** (scelto): controllabili, provabili sullo storico, freni sicuri.
2. Regole libere composte: flessibili ma facili da sbagliare e da provare male.
3. Codice del Quant: massima libertà, rischio alto. Scartato.

## Design

### Modelli (prima serie)
Ogni modello ha parametri validati (limiti min/max), una quantità (pezzi o importo) e uscite proprie:
- `breakout` — prezzo sopra il massimo degli ultimi N minuti (opz. volume del minuto ≥ k × media).
- `mean_reversion` — scarto dal prezzo medio mobile di N minuti oltre z deviazioni → entrata al rialzo.
- `vwap_pullback` — sopra il VWAP della seduta, ritorno entro x% dal VWAP → entrata.
Uscite comuni: stop %, obiettivo %, uscita a tempo (max secondi in posizione), uscita di fine seduta
(azioni). Finestre: niente entrate nei primi/ultimi M minuti della seduta; pausa dopo ogni uscita.
Solo acquisti (long). Nuovi modelli si aggiungono nel codice con test.

### Regole
Tabella `auto_rules`: portafoglio, simbolo/i, modello, parametri, quantità, stato (bozza / attiva /
in pausa / fermata dai freni), versioni (ogni modifica crea una versione; i risultati citano la
versione). Gestite dal portale (solo admin). Attivabili solo su portafogli con `auto_rules_enabled`.

### Motore
- **Processo dedicato** (servizio `scalper` nel compose, stessa immagine) per non rallentare worker e
  API; un solo proprietario dei flussi prezzi resta il componente attuale: il motore riceve i prezzi
  dal flusso esistente (pubblicazione interna, per esempio Redis), non apre connessioni proprie ai
  broker per i dati.
- Tiene in memoria, per simbolo, le barre da 1 secondo e da 1 minuto e gli indicatori dei modelli;
  al riavvio si riscalda dallo storico al minuto.
- A ogni prezzo valuta le regole attive del simbolo. Scatto → **ordine** con il percorso ordini
  esistente (controlli di rischio, PDT, kill switch, riduzione) e uscite tenute da SMTrading
  (come le uscite sintetiche di oggi), con `day_trade` per le azioni.
- **Guardie prima di ogni invio:** prezzo più vecchio di S secondi (default 2) → niente; forbice oltre
  soglia della regola (default 0,10%) → niente; mercato chiuso → niente; già una posizione o un
  ordine aperto della stessa regola → niente; idempotenza per (regola, scatto) contro i doppi invii.
- Entrate a mercato o a limite aggressivo (scelta del modello); uscite a mercato.


### Scelte di Claude (dopo la revisione del progetto)
1. **Tempi:** resta un solo componente che scrive gli ordini al broker (oggi la sincronizzazione ogni
   2 s); il motore lo **sveglia subito** a ogni nuovo ordine (notifica interna), così lo scatto →
   invio sta sotto il secondo senza rompere la regola dello scrittore unico. Il secondo è un obiettivo
   misurato in `auto_trades`, non un blocco.
2. **Volume e VWAP:** al flusso Alpaca esistente si aggiunge l'abbonamento alle **barre da 1 minuto**
   solo per i simboli delle regole attive (stessa connessione, nessun cambio per gli altri simboli);
   volume e VWAP sono al minuto. Con il flusso gratuito il volume è solo IEX: lo si dichiara nei risultati.
3. **Posti nel flusso prezzi:** vince la campagna. Il motore usa i posti liberi più una quota riservata
   (default 10) solo quando ci sono regole attive; una regola senza flusso non entra (guardia del prezzo
   vecchio) e il portale la mostra «senza flusso». Se il nuovo collegamento di prova ha un flusso
   proprio, il motore usa quello.
4. **Una regola attiva per (portafoglio, simbolo)**; ordini manuali su quel simbolo rifiutati finché
   la regola è attiva. Modo A e modo B non possono avere lo stesso simbolo sullo stesso portafoglio.
   Il budget PDT è unico per collegamento e conta tutti e due i modi.
5. **Uscite:** a ogni entrata eseguita si mettono subito stop e obiettivo come coppia sintetica a
   riposo, sorvegliata dal motore delle uscite esistente (resta attiva anche se il motore delle regole
   si ferma); uscita a tempo e di fine seduta le manda il motore delle regole, con la rete di fine
   seduta del worker come ultima garanzia.
6. **Un solo nuovo conto di prova (30.000 $)** per i due modi: il conteggio delle operazioni di
   giornata si confronta comunque con `daytrade_count` di Alpaca; i rifiuti si provano con
   `max_day_trades_5d`. Il blocco del broker sotto i 25.000 $ non si vede dal vivo (coperto dai test).
7. **Budget delle operazioni di giornata:** la regola USA è per collegamento (somma di tutti i
   portafogli e dei due modi); `max_day_trades_5d` conta solo le operazioni del proprio portafoglio.
   Sui collegamenti con margine sotto 25.000 $ il modo B **non si può accendere** (il portale lo
   rifiuta spiegando perché); le uscite a tempo e di fine seduta contano come ogni altra operazione
   di giornata e non sono mai rifiutate.
8. **Valuta dei freni di perdita:** quella del portafoglio (conversione con il cambio già usato dal
   portafoglio).

### Freni
Per regola e per portafoglio, dal portale: max operazioni per ora e per giorno, perdita massima del
giorno (€ e %), pausa dopo K perdite di fila, max posizioni aperte. Freno scattato → regola «fermata
dai freni» fino a riattivazione manuale, avviso Telegram. Il kill switch esistente ferma tutto.
Se il motore perde il flusso prezzi o il collegamento broker: niente nuove entrate, uscite ancora
garantite dal motore delle uscite sintetiche esistente.

### Misura
Tabella `auto_trades`: regola e versione, simbolo, scatto (ora, prezzo, valori), invio, esecuzione
(ora, prezzo), uscita (motivo, ora, prezzo), commissioni vere, risultato netto, scarto prezzo atteso →
eseguito, tempi in millisecondi. Portale: pagina «Regole automatiche» (regole, stato, freni, risultati
per regola e per giorno, operazioni). Telegram: riepilogo serale delle prove.

### Prova sullo storico
Il backtester riproduce i modelli sulle barre da 1 minuto (azioni Alpaca, cripto) con commissioni del
broker, forbice stimata e ritardo d'esecuzione impostabile; stessi conti del motore (codice condiviso
per indicatori e regole). Limite noto: al minuto non si vedono i movimenti dentro il minuto: lo si
dichiara nei risultati e lo si misura confrontando con la prova dal vivo.

### Portale
Pagina «Regole automatiche» (crea/modifica/attiva/pausa, freni, risultati); campi del portafoglio;
segno «di giornata» e riquadro PDT sulle posizioni; casella «di giornata» nel modulo ordini.

### Prove
- Unitarie: ogni modello (scatti e non scatti su serie note), guardie (prezzo vecchio, forbice,
  mercato chiuso, doppio scatto), freni, idempotenza, riscaldamento al riavvio, parità motore ↔
  prova sullo storico sulla stessa serie.
- Carico: il motore regge il flusso di 30 simboli senza ritardi sulle uscite esistenti.
- Non regressione: interruttore spento ovunque → nessuna differenza; impronta MCP invariata.
- Dal vivo (nuovo conto di prova, portafoglio nascosto senza agenti): alcune sedute con 1-2 regole su
  titoli liquidi e su 1-2 cripto; misura di tempi, scostamenti dei prezzi IEX, costi, risultati;
  prova dei freni e della rete di fine seduta; pulizia finale con cancellazione delle tracce.

## Lasciato a chi costruisce
Nomi esatti, protocollo di pubblicazione interna dei prezzi, struttura del processo `scalper`, limiti
dei parametri dei modelli, testi italiani, ordine dei lavori.
