# SMTrading — Piano 9: eToro e miglioramenti (design)

Data: 2026-11-15 · Stato: bozza da approvare (risposte dell'utente alla revisione incluse) · Ramo previsto: `feat/plan-09-etoro-improvements` (da `main` 045f598+)

## Perché
Dopo il piano 8 (denaro reale costruito, spento) l'utente vuole:
- un terzo broker, eToro, da usare come Alpaca e Trading 212: prova (demo) e reale;
- chiudere i miglioramenti emersi durante la prima settimana di uso.

Interactive Brokers è **rimandato**, per decisione dell'utente del 2026-11-15. Per i privati richiede un Client Portal Gateway locale con login, e secondo diversi sviluppatori il login va rifatto ogni giorno.

Tutto in un unico piano, per decisione dell'utente.

## Obiettivi
1. **eToro demo e reale**, con lo stesso modello di sicurezza del piano 8: TOTP per collegare e attivare, disattivazione a un clic, prova a secco, controlli pre-trade, riconciliazione, chiavi cifrate, guardie sugli host, registro append-only. Per gli agenti si usano solo acquisti e vendite **senza leva**: azioni, ETF e crypto reali a leva 1. Niente CFD, short o leva.
2. **Festività di borsa ovunque.** `instruments/market_hours.py` usa già `exchange_calendars` (NYSE, NASDAQ, MTA, XETRA, Parigi), quindi le festività sono già rispettate per l'apertura dei mercati. Mancano tre cose:
   - le regole che contano i giorni con il solo weekday, oggi `risk/live_checks.py` LAST_FILL_MAX_AGE_WEEKEND;
   - la Borsa di Londra (XLON) per eToro e T212;
   - le **chiusure straordinarie manuali** dall'admin, per esempio una chiusura improvvisa o un mezzo giorno non previsto dal calendario.
3. **Modulo ordini manuale completo** nel portale.
   - Comprare e vendere qualsiasi strumento su qualsiasi portafoglio, con gli stessi controlli di rischio degli agenti (stesso `place_order` dell'engine, `source="admin"`).
   - Anteprima prima dell'invio.
   - TOTP obbligatorio sui portafogli reali, dove compare `LiveOrderConfirm`, che oggi esiste ma non è usato.
   - Audit.
4. **Più valute nel reale.** Un portafoglio con valuta base diversa da quella del conto broker è ammesso: per esempio un portafoglio EUR su Alpaca (USD), o USD su un conto T212 o eToro in EUR. Oggi la regola R8.5 lo vieta.
5. **Sezione "Decisioni di oggi"** nel riepilogo delle 18:00 (`telegram/summaries.py`): le decisioni registrate nella giornata, scritte in chiaro (vedi §Riepilogo). Per decisione dell'utente si aggiungono **campi strutturati** a `log_decision` e si aggiornano le istruzioni degli agenti (vedi §Riepilogo).
6. **Costi degli agenti in SMTrading.** Lettura periodica di Paperclip, in sola lettura, per mostrare il costo per agente e per portafoglio accanto ai risultati.
7. **Allarme isolamento Paperclip.** Lo stesso lettore confronta le policy di blocco "Isolate … SMTrading" con quelle attese e, se cambiano, avvisa su Telegram e nel portale.
8. **Pulizia dei nomi.**
   - Rinominare i dati admin esistenti che contengono parole da simulazione e che gli agenti potrebbero vedere: i portafogli archiviati «Test 250 · …», «Paperclip», «Demo» e gli agenti «paperclip-demo», «smoke-agent».
   - Aggiungere un controllo permanente che avvisa l'admin quando salva un nome visibile agli agenti contenente queste parole.

## Non-obiettivi
- Interactive Brokers (rimandato).
- Su eToro: CFD, leva maggiore di 1, short, copy trading, prelievi e depositi via API.
- Attivare il denaro reale: anche eToro reale viene costruito e resta **spento**. L'attivazione segue la checklist del README, insieme all'utente.
- Scrivere in Paperclip: SMTrading lo legge soltanto.
- Cambiare il comportamento degli agenti o le loro istruzioni, **tranne** l'uso dei nuovi campi di `log_decision` (approvato dall'utente il 2026-11-15). Le istruzioni potranno citare eToro solo dopo una nuova approvazione dell'utente.
- Negoziare titoli della Borsa di Londra: si aggiungono solo orari e festività, i titoli `.L` restano non negoziabili (decisione dell'utente).

## Anti-obiettivi (fallimento anche se "funziona")
- Un ordine eToro che apre una posizione a leva maggiore di 1, o uno short.
- Chiavi eToro (API key o user key) in chiaro in log, database, risposte, CSV, Telegram, MCP o errori.
- Gli agenti che vedono differenze tra demo e reale, o parole da simulazione. Valgono le stesse regole di `mcp_hide_mode` e il test `test_mcp_no_fingerprints` va esteso a eToro.
- La chiave board di Paperclip esposta nel portale, o usata per scrivere.
- Un ordine manuale che salta i controlli di rischio o il TOTP sul reale.
- Una conversione di valuta che fa superare il contante reale disponibile.

## Approcci considerati (scelti dall'utente il 2026-11-15)
| Tema | Scelto | Scartato e perché |
|---|---|---|
| eToro | **Adattatore nostro** sull'API ufficiale, con le stesse guardie di `brokers/live` | Libreria non ufficiale `etoropy`: codice esterno che maneggia le chiavi |
| Paperclip | **Job periodico nel worker ogni 5 minuti**, in sola lettura | Lettura solo all'apertura della pagina: non darebbe allarmi automatici |
| Festività | **Calendari `exchange_calendars`** (già usati) più **chiusure manuali** dall'admin | Tabella manuale annuale: fragile |
| Broker | Solo eToro adesso | IBKR: gateway con login quotidiano, rimandato |
| Ordine dei lavori | Un unico piano | Più piani in sequenza |

## Design

### eToro
- **Tipi di collegamento:** `etoro_demo` (prova, come `alpaca_paper` e `t212_demo`) ed `etoro_live` (reale, nel gruppo `LIVE_KINDS`).
- **API:** pubblica eToro, `https://public-api.etoro.com`. Si autentica con l'intestazione `x-api-key` (chiave applicazione) più `x-user-key` (chiave utente, diversa tra ambiente demo e reale). Le esecuzioni demo e reali hanno percorsi distinti (`…/execution/demo/…` e `…/execution/…`).
  - **Da verificare sul portale ufficiale prima di scrivere codice:** versione (v1 o v2), percorsi esatti, idempotenza e chiave client, limiti di frequenza, unità degli ordini, chiusura parziale, stato degli ordini.
  - Il builder fissa questi dettagli in un "documento protocollo" del piano, con fonti, e il piano li usa nei test con un server finto.
- **Traduzione del modello.**
  - Acquisto = apertura di una posizione **a leva 1, direzione buy**, per importo o per quantità, secondo quanto l'API permette.
  - Vendita = chiusura totale o parziale delle posizioni di quello strumento, in ordine FIFO.
  - Gli ordini limit e stop sono ammessi solo se l'API li supporta con leva 1. Altrimenti vengono rifiutati con `INVALID_ORDER` e messaggio in chiaro.
  - Short e leva maggiore di 1: rifiutati prima dell'invio, sia in demo sia in reale.
- **Strumenti:** mappatura simbolo SMTrading ↔ instrumentId eToro, con cache come per T212. Se uno strumento non è negoziabile su eToro senza leva, viene rifiutato.
- **Reale:** si riusa tutto `brokers/live`:
  - cancello `gate.py`;
  - stati configured, dry_run, armed;
  - disattivazione in due fasi;
  - fusibile ordini all'ora;
  - controlli pre-trade (contante, limiti);
  - riconciliazione con debounce (R8.7, R8.8);
  - impronta del conto;
  - riduzione sola su ordine sconosciuto (M8);
  - avvisi Telegram;
  - guardia sugli host live, a cui si aggiunge l'host eToro reale. Il percorso demo usa lo stesso host ma URL diversi: la guardia deve distinguere per percorso, `/demo/`. **Rischio da coprire con test:** un adattatore demo non deve mai poter chiamare un URL di esecuzione reale, e viceversa.
- **Conto dedicato** (decisione dell'utente): sia il demo sia il reale si collegano solo se **vuoti**, senza posizioni né ordini aperti, come per gli altri broker (NOT_DEDICATED). Nessuna "base esterna". Per il demo, la guida indica come azzerare il conto virtuale da eToro prima di collegarlo.
- **Vendita parziale** (scelta del controller): se l'API non permette di chiudere parzialmente una posizione, ogni acquisto SMTrading apre una posizione eToro distinta. Una vendita chiude, in ordine FIFO, le posizioni intere fino alla quantità richiesta. L'eventuale resto viene registrato come esecuzione parziale e l'ordine si chiude con quanto eseguito, come una normale esecuzione parziale di un broker: nessun codice o messaggio specifico di eToro.
- **Conto:** saldo, contante disponibile, valuta e posizioni (`account.py`), con requisito "conto senza leva" equivalente a R8.10. Se l'API espone le impostazioni di leva o il tipo di conto, lo si verifica all'attivazione; altrimenti lo si documenta e la leva 1 si impone per ordine.
- **Portale:** la procedura guidata del piano 8 offre eToro come terza opzione, con la guida ai permessi delle chiavi e i link al portale API di eToro.

### Festività
- In `market_hours.py` si aggiunge Londra (`LSE`: `XLON`) **solo per orari e festività**. I titoli londinesi restano non negoziabili, `instruments/discovery.py` invariato (decisione dell'utente).
- Nuova tabella `market_closures`: mercato, data, tipo (chiuso o chiusura anticipata con orario), nota, autore. Ha la precedenza sul calendario. Si gestisce nella pagina Mercati, con audit.
- `market_status` considera le chiusure manuali.
- Le regole basate sul weekday (`live_checks` LAST_FILL_MAX_AGE*) diventano "un giorno di borsa" del mercato dello strumento, calcolato dal calendario.
- Si cercano altri usi di `weekday()` legati ai mercati (lo fa il builder) e si convertono.

### Modulo ordini manuale
- **Pagina:** Portafoglio → «Nuovo ordine». Campi: simbolo con ricerca, lato, tipo (market, limit, stop, stop-limit, trailing, come gli agenti), quantità o importo, prezzi, durata.
- **Flusso:** «Anteprima», che usa la stessa logica di `preview_order`, poi «Invia».
- **Reale:** `LiveOrderConfirm` chiede il TOTP e mostra «🔴 denaro reale» con importo e valuta.
- **API admin:** `POST /api/portfolios/{id}/orders` (preview e place), che usa lo stesso percorso dell'engine con `source="admin"` e `agent_id=null`. Si applicano tutti i controlli di rischio, il kill switch e il trading halt, che vincolano anche l'admin. Per lo halt c'è una sola eccezione: le chiusure, come per gli agenti. Audit admin.
- **Annullamento ordini aperti** dal portale (se non esiste già): un clic, senza TOTP. Annullare riduce il rischio.

### Più valute nel reale
- **Regola R8.5:** la stessa valuta non è più obbligatoria. Resta la banda di cambio dell'1% come margine di sicurezza.
- **Contante e limiti:** si convertono con il cambio corrente (`fx` del market data router). Il margine di sicurezza sul contante usa il cambio peggiore tra corrente e ultimo noto più l'1%.
- **Riconciliazione:** confronta nella valuta del broker. L'equity del portafoglio virtuale resta nella sua valuta base.
- **Scarti dovuti al cambio** (decisione dell'utente): una differenza di contante spiegata dal solo movimento del cambio, cioè contante virtuale convertito al cambio corrente contro contante del broker, con posizioni coincidenti, produce **solo un avviso** («scarto di cambio») su Telegram e nel portale, al massimo uno al giorno. **Non** mette il portafoglio in sola chiusura. Le differenze vere (posizioni diverse, contante mancante oltre lo scarto di cambio più la banda dell'1%) seguono R8.7 e R8.8 come oggi.
- **Senza cambio** (provider FX non disponibile): nessun nuovo acquisto in valuta diversa, l'ordine attende come oggi `_WAIT` e scade dopo 30 minuti. Le vendite sono sempre ammesse.
- **Commissioni FX** del broker (T212 0,15%, eToro secondo listino): si modellano con le regole di commissione esistenti, configurabili dall'admin.

### Riepilogo delle 18:00: «Decisioni di oggi»
- **Campi strutturati in `log_decision`** (decisione dell'utente), tutti opzionali e retrocompatibili:
  - `outcome`: `approvata`, `respinta`, `sospesa`, `modificata`, `informativa`;
  - `reason`: testo breve, al massimo 300 caratteri;
  - `subject`: descrizione in chiaro di cosa si decide, per esempio «incrocio medie mobili 10/30 su SPY, QQQ e DIA», al massimo 200 caratteri.
  - Nuove colonne in `decisions`, con migrazione. Lo schema dello strumento MCP e `get_decisions` li espongono.
  - Le istruzioni degli agenti (regole comuni §9 `log_decision`) si aggiornano con una nuova versione dalla pagina «Istruzioni agenti»: «compila sempre outcome, reason e subject». Il testo esatto va mostrato all'utente prima della pubblicazione.
- **La sezione nel riepilogo giornaliero** (e settimanale per la settimana) raggruppa per tipo:
  - strategie;
  - rischio;
  - allocazioni;
  - veti e approvazioni.
- **Una riga per decisione:** «✅/❌/⏸ <subject> — <portafoglio> — <reason>», con l'agente in chiaro.
  - Se i campi mancano, perché la decisione è di un agente non aggiornato o è vecchia, si usa `summary` accorciato.
  - Gli id interni (uuid, numeri di decisione) non compaiono.
  - I codici come STR-001 compaiono solo accanto a `subject`.
- **Testo** da modelli fissi in italiano, niente IA. Al massimo 15 righe, poi «… e altre N».

### Lettura di Paperclip (costi e isolamento)
- **Configurazione** nel portale, nuova sezione «Paperclip» in Impostazioni o Connettori: URL base, chiave board (cifrata come le altre, mai mostrata, solo mascherata), id azienda, interruttore. Il pulsante «Verifica» fa una lettura di prova.
- **Job nel worker ogni 5 minuti**, solo richieste GET:
  1. **Costi:** `GET /api/companies/:cid/costs/by-agent` più le esecuzioni da `heartbeat-runs`. Ogni esecuzione ha `usageJson.costUsd` e `billingType`, quindi l'equivalente delle esecuzioni in abbonamento è il `costUsd` che Paperclip già calcola (scelta del controller). Gli importi si mostrano in **dollari**, come in Paperclip, distinguendo «a consumo API» da «incluso nell'abbonamento». Si salva uno snapshot per agente e giorno.
  2. **Isolamento:** `GET /api/companies/:cid/tools/policies`, confrontato con l'insieme atteso. L'insieme atteso si registra al primo avvio ("fotografa la configurazione attuale") e si riconferma dall'admin con un pulsante. Policy rimosse, disattivate o modificate generano un avviso critico su Telegram (categoria «system») e un banner nel portale.
  3. **Agenti:** l'abbinamento agente SMTrading ↔ agente Paperclip è per nome, modificabile nel portale.
- **Pagina «Costi agenti»:** costo per agente e per giorno, settimana e mese, accanto a ordini e rendimento dei portafogli su cui l'agente ha permessi.
- **Errori di lettura:** Paperclip spento o chiave errata. Si mostra lo stato e si avvisa una volta dopo 3 errori consecutivi. Niente allarmi a raffica.
- **La chiave board** non finisce mai nei log. Le risposte di Paperclip si salvano solo nei campi necessari.

### Pulizia dei nomi
- **Una tantum:** il piano include un comando CLI (`python -m smtrading.cli names audit|fix`) che elenca e, su conferma, rinomina portafogli e agenti con parole vietate. I nomi proposti sono neutri, per esempio «Portafoglio archiviato 1…4» e «Agente archiviato 1–2». Il controller lo esegue in produzione **solo dopo** l'approvazione dell'utente dei nomi proposti.
- **Permanente:** al salvataggio di un portafoglio, di un agente o di una regola visibile agli agenti, se il nome contiene `test|demo|paper|sim|prova|fake|smoke` il portale mostra un avviso, non un blocco: «gli agenti vedranno questo nome». Lo stesso elenco di parole è usato da `test_mcp_no_fingerprints`, in un solo modulo.

## Vincoli
- **Stesso stack e stesse regole del progetto:** TDD, revisione di ogni task, deploy solo da un worktree pulito di HEAD. Con migrazioni, `docker compose run --rm migrate` prima di `up --no-deps`.
- Migrazioni lineari dopo la 0021.
- **Nessuna attivazione del reale.** eToro reale resta spento: 0 collegamenti `etoro_live` in produzione.
- `mcp_hide_mode` e tutte le regole del piano 8 (R8.4–R8.13) restano valide.
- **Per gli agenti:**
  - nuovi codici d'errore solo se indistinguibili da un broker reale;
  - le istruzioni non cambiano in questo piano senza ok dell'utente.

## Lasciato al builder
- I dettagli del protocollo eToro, da verificare sul portale ufficiale e documentare con fonti nel piano.
- Nomi esatti di tabelle e colonne, numero delle migrazioni, posizione della sezione Paperclip nel portale.
- Il formato esatto delle righe «Decisioni di oggi».
- L'ordine dei task, purché il denaro reale eToro arrivi dopo eToro demo e dopo le guardie.
