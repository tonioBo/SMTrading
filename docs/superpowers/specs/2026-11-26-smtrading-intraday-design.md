# SMTrading — Lavoro in giornata (intraday)

Data: 2026-11-26 · Stato: BOZZA da approvare · Ramo: `feat/intraday` (worktree), in linea solo **spento**.

## Perché
Matteo (26/11/2026) vuole che, dopo la campagna di dicembre e il sì/no del 23/12, gli agenti possano
lavorare anche dentro la giornata (segnali su barre brevi, entrate e uscite nella stessa seduta).
Vuole che SMTrading sia **già pronto** a fine campagna: tutto costruito e provato, da accendere a gennaio.
Oggi i segnali sono quasi tutti su barre giornaliere (168 prove `1d`, 2 `1h`), il Portfolio Manager
guarda i portafogli circa ogni ora e solo stop e obiettivi sono sorvegliati in continuo.

## Decisioni dell'utente
- **Fin dove ora:** tutto costruito spento e provato su un portafoglio di prova nascosto su Alpaca;
  a gennaio basta accendere e scrivere le istruzioni.
- **Allo scatto di un segnale:** SMTrading **sveglia l'agente** (apre un compito al Portfolio Manager
  del portafoglio), che decide e ordina come oggi. Niente ordini «armati» eseguiti da SMTrading
  (si possono aggiungere dopo se il ritardo costa).
- **Fine seduta:** le posizioni di giornata le chiude il Portfolio Manager al ciclo di pre-chiusura;
  **rete di sicurezza:** quelle ancora aperte le chiude SMTrading pochi minuti prima della fine,
  a mercato aperto (non è la chiusura fuori orario esclusa dal prodotto).
- **Mercati:** progetto generico, prove sulle azioni USA con il conto di prova Alpaca; Europa/eToro dopo.
- **Prezzi in tempo reale:** si valuta una fonte a pagamento; Claude propone le opzioni con costi e
  copertura e **chiede prima** di attivarne una.
- **Conto della prova:** un **nuovo conto di prova Alpaca** con saldo piccolo (sotto 25.000 $), aperto
  da Luca/Matteo, con un collegamento broker proprio: la prova non tocca il conto del Portafoglio
  Alpaca (conteggi, riconciliazione, flusso prezzi) e la regola USA si prova davvero.

## Obiettivi
1. Sorveglianza dei segnali di giornata con sveglia dell'agente.
2. Regola USA sui conti piccoli (PDT) contata e applicata da SMTrading.
3. Posizioni «di giornata» riconoscibili, chiuse dall'agente e, in mancanza, da SMTrading prima della fine.
4. Storico e prezzi a barre brevi sufficienti per prove e segnali.
5. Impostazioni per portafoglio (lette da SMTrading, nessun valore nelle istruzioni).
6. Prova completa su un portafoglio di prova nascosto e voce nel documento di prontezza.

## Non obiettivi
- Ordini condizionati eseguiti da SMTrading senza l'agente.
- Europa/eToro in giornata, opzioni, vendite allo scoperto in giornata.
- Istruzioni agli agenti e accensione: dopo il 23/12, con domanda all'utente.

## Anti-obiettivi (sarebbe un fallimento anche se funziona)
- Gli agenti vedono qualcosa di nuovo durante la campagna (strumenti, campi, testi, compiti).
- Una messa in linea disturba la campagna (fatta con agenti attivi, o cambia il comportamento attuale
  dei portafogli giornalieri).
- Valori fissi (portafogli, simboli, orari, soglie) nelle istruzioni invece che in SMTrading.
- Una chiusura di sicurezza a mercato chiuso o su una quotazione con forbice larga.

## Vincoli
- Tutto dietro un interruttore per portafoglio, **spento** di default: con l'interruttore spento il
  comportamento di oggi non cambia (test di non regressione).
- Gli strumenti MCP **non cambiano** finché non si accende: niente nuovi parametri o testi visibili
  agli agenti. La prova si guida dal portale/API interne (come le prove reali di novembre).
- Messa in linea solo ad agenti fermi; backup prima; prove con test.
- Il portafoglio di prova non ha accesso agenti e le sue tracce si cancellano a fine prova.

## Approcci considerati
1. **Sveglia dell'agente** (scelto): a ogni chiusura di barra SMTrading ricalcola i segnali di
   giornata e apre un compito al Portfolio Manager. Decisioni agli agenti, istruzioni quasi uguali;
   1-3 minuti di ritardo (adatto a 15m/1h, lento per 5m); costo che cresce con gli scatti.
2. **Ordine armato:** l'agente prepara prima l'ordine condizionato, SMTrading lo esegue allo scatto.
   Immediato ed economico, ma l'agente decide senza vedere il momento: regola nuova. Scartato per ora.
3. **Entrambi:** il più flessibile, il più lungo. Rimandato.

## Design

### Impostazioni per portafoglio (scelta di Claude: sul portafoglio, non sul profilo)
I profili di rischio sono condivisi (chi non ne ha uno usa «Default»): un interruttore lì accenderebbe
anche i portafogli della campagna. Quindi nuovi campi su `portfolios`, tutti opzionali:
- `intraday_enabled` (bool, default false) — l'interruttore.
- `intraday_timeframes` — barre ammesse (sottoinsieme di 5m/15m/1h; default 15m e 1h).
- `intraday_wake_agent_id` — l'agente da svegliare agli scatti (scelto tra gli agenti con permesso
  `trade` sul portafoglio; il nome Paperclip si risolve dall'agente). Senza valore: nessuna sveglia,
  scatti solo registrati e avviso «system».
- `day_close_minutes` — minuti prima della fine seduta per la chiusura di sicurezza (default 10).
- `max_day_trades_5d` — tetto proprio di operazioni di giornata su 5 sedute (facoltativo, oltre la regola USA).
- `intraday_max_wakes_per_hour` (default 6).
Il portale li mostra e modifica nella pagina del portafoglio (solo admin).

### Sorveglianza dei segnali (job del worker)
- Si appoggia al servizio segnali esistente (`signals/service.py`): stessa fonte delle strategie
  (decisioni del CEO), stesse regole d'entrata/uscita e filtri del backtester.
- Considera solo strategie con timeframe di giornata ammesso, su portafogli con `intraday_enabled`.
- Gira a ogni **chiusura di barra** del timeframe (allineata all'orario di borsa del mercato del
  simbolo, solo a mercato aperto, festività comprese), con un piccolo ritardo perché la barra sia
  disponibile dal fornitore.
- Uno **scatto** = entrata (`triggered_closed` passa da falso a vero, senza posizione né entrata a
  riposo) o uscita (regola d'uscita vera con posizione aperta). Lo stato precedente per
  (strategia, portafoglio, simbolo, regola) è in Redis, così un segnale che resta vero non sveglia
  di nuovo; ogni scatto è registrato (tabella `intraday_triggers`, append-only) con valori e barra.
- **Sveglia:** compito Paperclip all'agente `intraday_wake_agent_id` del portafoglio (stesso scrittore
  di compiti della protezione, `paperclip/issues.py`, generalizzato al destinatario) con titolo e testo
  generati dai dati (strategia, simbolo, regola, valore, soglia, prezzo, ora della barra). Nessun nome
  fisso nel codice. Limiti anti-tempesta: al più un compito aperto per
  (portafoglio, simbolo); tetto `intraday_max_wakes_per_hour` per portafoglio, oltre il
  quale gli scatti si registrano senza sveglia e il riepilogo serale li riporta.
- Sul portafoglio di prova (senza agenti) la sveglia è sostituita da un messaggio Telegram «system»
  con lo stesso testo: serve a verificare la sorveglianza.

### Posizioni di giornata
- Un'entrata è «di giornata» se l'ordine porta il segno `day_trade` (nuovo campo `orders.day_trade`,
  bool). Durante la campagna lo impostano solo il portale e le API interne; lo strumento MCP lo
  riceverà all'accensione.
- Una posizione è di giornata finché resta quantità comprata in giornata con quel segno nella seduta
  corrente (calcolo dai riempimenti del giorno, nessuno stato nuovo da tenere allineato).

### Chiusura di fine seduta
- Pre-chiusura: il ciclo dell'agente esiste già (orari dalle routine); all'accensione le istruzioni
  diranno di chiudere le posizioni di giornata (testo generico).
- **Rete di sicurezza** (job del worker): `day_close_minutes` prima della fine della seduta del
  mercato del simbolo (calendario esistente, mezze giornate comprese), per ogni posizione di giornata
  ancora aperta: annulla stop/obiettivo collegati e manda una vendita a mercato della quantità di
  giornata, con `source` di sistema e motivo leggibile. Mai a mercato chiuso; con forbice oltre
  MAX_SPREAD usa un limite al prezzo dell'ultimo scambio meno un margine (dal profilo, default 0,5%),
  e se non esegue entro la fine avvisa su Telegram. Un avviso Telegram per ogni chiusura di sicurezza.

### Regola USA sui conti piccoli (PDT)
- Si applica per collegamento broker: conto con margine e patrimonio sotto 25.000 $ (dato letto dal
  broker; Alpaca espone anche il proprio conteggio `daytrade_count` e `pattern_day_trader`, che
  SMTrading confronta con il suo e registra in caso di differenza).
- Un'operazione di giornata = acquisto e vendita dello stesso titolo nella stessa seduta (conteggio
  dai riempimenti, 5 sedute mobili, calendario USA).
- Il conteggio è per collegamento e include tutti i riempimenti del conto (anche di portafogli
  giornalieri sullo stesso collegamento: per il broker contano allo stesso modo).
- Controllo di rischio prima dell'invio (scelta di Claude): si rifiuta l'**entrata** — un acquisto
  segnato `day_trade` su un portafoglio con `intraday_enabled` — quando le operazioni disponibili
  nelle 5 sedute sono finite (3 meno quelle usate, o `max_day_trades_5d` se più basso), con codice
  proprio e testo che spiega quante ne restano e quando si libera la prossima. Le **vendite non sono
  mai rifiutate** (rifiutarle lascerebbe la posizione alla rete di fine seduta, che farebbe comunque
  l'operazione). Sui portafogli senza `intraday_enabled` nessun rifiuto, solo un avviso Telegram
  quando una vendita nella stessa seduta dell'acquisto porterebbe al limite (non regressione).

### Storico e prezzi
- Storico a barre brevi da Alpaca (già supportato per 1m/5m/15m/1h): verificare profondità e limiti
  del conto gratuito (IEX) e usarlo nel backtester per le prove del Quant; cache come per le barre
  giornaliere.
- Fonte in tempo reale: Claude prepara un confronto (flusso completo Alpaca/SIP e alternative: costo
  mensile, borse coperte, numero di simboli, ritardo) e chiede all'utente. Fino ad allora la prova
  usa il flusso gratuito, segnando nella pagella quanto i prezzi IEX differiscono.

### Portale
- Profilo di rischio: i nuovi campi.
- Pagina segnali: colonna/filtro «in giornata» e ultimi scatti con l'esito (compito aperto, saltato
  per tetto, nessun agente).
- Posizioni: segno «di giornata»; conteggio PDT per collegamento (usate / restanti / quando si libera).
- Modulo ordini manuale: casella «di giornata».

### Prove
- Unitarie: allineamento delle chiusure di barra e mezze giornate; transizioni di scatto e
  ripetizioni; tetto dei compiti; conteggio PDT (bordi: 5 sedute con festività, quarta operazione,
  uscite mai bloccate); rete di fine seduta (mercato chiuso, forbice larga, quantità parziale).
- Non regressione: con `intraday_enabled` spento ovunque, nessuna differenza per i portafogli attuali
  e per gli strumenti MCP (confronto degli schemi degli strumenti).
- Prova reale (nuovo conto di prova Alpaca con collegamento proprio, portafoglio nascosto senza
  agenti, nel calendario delle esercitazioni; il flusso prezzi del nuovo collegamento non deve
  togliere simboli a quello della campagna): una strategia 15m su 1-2 titoli liquidi per alcune sedute; verificare scatti
  e messaggi, un'entrata e un'uscita di giornata manuali, il conteggio PDT confrontato con Alpaca,
  la rete di fine seduta (lasciando apposta una posizione aperta), nessun disturbo alla campagna.
  Esito nel documento di prontezza; pulizia finale (conto vuoto, tracce cancellate).

## Lasciato a chi costruisce
Nomi esatti di tabelle/campi/codici d'errore, struttura del job (uno o due), formato dei testi dei
compiti e dei messaggi (in italiano, chiari), dettagli della cache delle barre brevi, ordine dei lavori.
