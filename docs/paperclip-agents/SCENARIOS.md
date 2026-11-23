# Catalogo degli scenari

Per ogni situazione che può capitare: chi se ne accorge, cosa deve succedere, cosa fa
SMTrading da sé e dove le istruzioni lo dicono. È un documento di controllo per chi
gestisce il sistema (come [`HANDOFFS.md`](HANDOFFS.md)): **non** va incollato negli agenti.
Verificato sul comportamento reale di SMTrading (motore ordini, processore, allarme di
protezione, obiettivi, split, perdita giornaliera, sorveglianza dei passaggi) il 18/11.

Legenda: **Coperto** = già nelle istruzioni · **Lacuna corretta** = mancava o era ambigua,
corretta il 18/11 nel file indicato · PM, RM, ES, CEO, MA, NA, QR, PA, NOT = agenti come nel
README · `00 §7` = file `00-regole-comuni.md`, sezione 7 · `#35` = riga della matrice di
`HANDOFFS.md`. Dal 19/11 i casi rari citati qui sono nell'appendice `10-appendice.md`
(sezioni «§<id>», richiamate dalle istruzioni con «→ §<id>»): il testo è lo stesso, spostato.

Principi applicati (decisioni del titolare del 18/11, `00` §2 e §4):

- **Limiti solo dal profilo di rischio**, letti a ogni ciclo con `get_risk_limits`; un
  limite `null` = lo decide l'agente (il RM con i suoi cinque controlli, motivati in
  `log_decision`). Nessun numero di limite nelle istruzioni.
- **Team autonomo**: ogni decisione ha un agente che la prende; gli umani sono solo
  informati; conflitti e priorità li arbitra il CEO. Quello che solo gli amministratori
  possono fare è un limite entro cui lavorare, non una decisione da attendere.

## A. Segnali d'ingresso

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| A1 | Segnale su barra `1d` chiusa (USA 22:00, UE 17:30) | PM, ciclo 22:15 | `get_indicators` 1d di tutte le strategie → PROPOSTA al RM per l'apertura successiva | — | Coperto: `02` «Chiusura USA (serale)» 1-2 |
| A2 | Segnale sulla barra in formazione prima della chiusura | PM, cicli nella finestra pre-chiusura | `include_forming: true`; PROPOSTA con `valid_until: "close"`; RM lo aggiunge se manca | non eseguito entro la chiusura → `expired`, uscite annullate | Coperto: `02` sentinella 7, `03` PROPOSTA 5, `04` ESEGUI 6 |
| A3 | Proposta serale eseguita all'apertura con un gap | PM 22:15, RM, ES | ingresso **`limit`** al prezzo massimo accettabile + `valid_until` ISO; `market` a mercato chiuso → veto RM / ES non esegue | un `market` in coda si esegue al prezzo d'apertura qualunque sia il gap | **Lacuna corretta**: `02` «Chiusura USA» 2, `03` PROPOSTA 5, `04` ESEGUI 4, `#32` |
| A4 | Azioni UE (lotto intero, ritardo ~15 min) vs USA (frazionarie) | PM, RM | qty intera, ingresso `limit` per UE; MA segnala azione UE troppo cara | `STALE_QUOTE` sui market con quotazione vecchia | Coperto: `00` §8.7-8, `02` checklist 6-7, `05` regola 9 |
| A5 | Crypto: la barra `1d` chiude alle 00:00 UTC, weekend | PM, primo ciclo del giorno (09:05, weekend 10:05) | ingressi e uscite delle strategie crypto sull'ultima barra chiusa; niente pre-chiusura né `"close"` | `valid_until: "close"` rifiutato su crypto e FX | **Lacuna corretta**: `02` sentinella 8 «crypto» |
| A6 | Strategia appena approvata | PM, task STRATEGIA APPROVATA | alert, watchlist **e** verifica subito della regola d'ingresso; limiti d'uso letti dalla decisione | — | **Lacuna corretta**: `02` «STRATEGIA APPROVATA» |
| A7 | Ciclo 22:15 non partito (Paperclip fermo, task aperto) | PM, 09:05 | prima i punti 1-2 della «Chiusura USA» sull'ultima barra chiusa | — | **Lacuna corretta**: `02` «Chiusura USA», `00` §6 «Ciclo saltato» |
| A8 | Chiusura anticipata, chiusura straordinaria, ora legale USA/UE sfasata | PM | finestra pre-chiusura = l'ora prima della chiusura reale di `get_market_status`, annotata alle 09:05 | `"close"` usa già la chiusura reale | **Lacuna corretta**: `02` sentinella 7, `#1` |
| A9 | Posto liberato da una chiusura (stop, target, uscita) | PM, stesso ciclo | rivaluta subito gli ingressi della strategia | — | Coperto: `02` sentinella 8 |
| A10 | Simbolo già in portafoglio (altra strategia o senza strategia) | PM, RM | nessun secondo ingresso su quel simbolo in quel portafoglio | una sola posizione per simbolo e portafoglio | **Lacuna corretta**: `00` §7 «Posizioni e uscite» 1, `02` sentinella 8, `03` PROPOSTA 6 |

## B. Esecuzione dell'ingresso

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| B1 | Ingresso eseguito | ES (`get_order`), PM/RM (feed `order.filled`) | ES risponde ESITO e chiude; uscite attive | Telegram «eseguito» | Coperto: `04` ESEGUI 7-9 |
| B2 | Ingresso eseguito in parte (solo presso un broker) | allarme di protezione → RM | PROTEGGI: cancellare il resto dell'ingresso o attendere, mai un OCO doppio | stop/target attivi a ingresso completo o scaduto | Coperto: `03` PROTEGGI, `00` §7 «Posizioni» 6 |
| B3 | Ingresso non eseguito, in attesa | PM, ogni ciclo | fuori validità → «ANNULLA» all'ES; altrimenti attende | — | Coperto: `02` sentinella 3, `04` ANNULLA |
| B4 | `valid_until` passato senza esecuzione | PM (feed `order.expired`) | nessuna azione; `log_decision` other se proposta ritirata | ingresso `expired`, uscite annullate | Coperto: `#34` |
| B5 | Rifiuto all'invio: `INVALID_ORDER` (anche `valid_until`), `RISK_LIMIT_EXCEEDED`, `INSUFFICIENT_BUYING_POWER`, `LIVE_CASH_LIMIT`, `SYMBOL_NOT_ALLOWED`, `SHORT_NOT_ALLOWED`, `STALE_QUOTE`, `PROVIDER_UNAVAILABLE`, `KILL_SWITCH_ACTIVE`, `TRADING_HALTED`, `REDUCE_ONLY`, `PORTFOLIO_LOCKED`, `NOT_FOUND`, `FORBIDDEN`, `RATE_LIMITED`, `INTERNAL` | ES | non esegue, restituisce al RM con `code`; reazione per codice | ordine registrato `rejected`; Telegram | Coperto: `00` §12, `04` «Errori comuni» |
| B6 | `LIVE_NOT_ARMED` (collegamento al broker non operativo) | ES | ESCALATION informativa; niente aperture per la giornata; dal giorno dopo flusso normale | — | **Lacuna corretta** (ripresa senza attesa): `00` §12 |
| B7 | Ingresso rifiutato **dopo** l'accettazione (es. `STALE_QUOTE` 10 min dopo l'apertura, rifiuto del broker) | PM (feed `order.rejected`) | proposta chiusa; reazione per codice; se il segnale vale ancora nuova `-v2` | rifiuto + annullamento delle uscite | **Lacuna corretta**: `02` sentinella 3 |
| B8 | ESEGUI non preso in carico dall'ES | RM (sentinella), sorveglianza passaggi | sollecito al ciclo dopo, al 2° task al CEO | Telegram «Passaggio mancante» dopo 30 min a mercato aperto | **Lacuna corretta**: `03` sentinella 4 (task creati da te) |
| B9 | 3 rifiuti in 24 h o stesso codice 2 volte | RM, ES | ESCALATION informativa | — | Coperto: `00` §11.3 |

## C. Uscite

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| C1 | Regola d'uscita della strategia soddisfatta | PM (09:05, 15:35, pre-chiusura, 22:15) | CHIUSURA totale al RM → CHIUDI all'ES | chiusura annulla le uscite OCO | Coperto: `02` sentinella 8 |
| C2 | Stop eseguito | PM/RM (feed) | rivaluta ingressi; RM controlla perdita giornaliera | target annullato (OCO), Telegram | Coperto: `02` sentinella 8, `03` sentinella |
| C3 | Target eseguito | come C2 | come C2 | stop annullato | Coperto |
| C4 | Gap oltre lo stop all'apertura | RM, primo ciclo dopo l'apertura | verifica esito e perdita giornaliera; nessun ordine prima | lo stop si esegue al primo prezzo, peggiore dello stop | **Lacuna corretta**: `00` §7 «Posizioni» 4, `03` sentinella 4 |
| C5 | Prezzo after-hours/pre-market già oltre lo stop | RM (`extended`) | annota la perdita stimata; **nessun ordine e nessun task** | nessuna esecuzione fuori seduta | **Lacuna corretta** (era «valuta con il PM»): `03` sentinella 4 |
| C6 | Chiusura parziale (CHIUSURA parziale, RIDUZIONE RISCHIO, rischio) | RM, ES | CHIUDI con stop/target per la qty restante; ES rimette subito un OCO sulla qty restante | `close_position` annulla **tutte** le uscite del simbolo | **Lacuna corretta**: `00` §7 «Posizioni» 2, `03` CHIUSURA, `04` CHIUDI 3, `#41` |
| C7 | Chiusura chiesta a mercato chiuso (22:15, weekend) | ES, poi allarme di protezione → RM | nessun OCO sopra una chiusura in coda; PROTEGGI chiuso con l'id della chiusura | chiusura `queued` fino all'apertura, uscite già annullate, allarme «senza stop» | **Lacuna corretta**: `00` §7 «Posizioni» 3, `03` PROTEGGI, `04` PROTEGGI 1 |
| C8 | Posizione intraday a fine giornata | PM (ultimo ciclo ≥ 30 min prima), RM (riserva) | CHIUSURA entro 16:05 UE / 21:05 USA (orari reali); negli ultimi 30 min PROTEGGI → CHIUDI invece dell'OCO | allarme `tif day` negli ultimi 30 min | **Lacuna corretta**: `02` regola 5, `03` sentinella 7 e PROTEGGI |
| C9 | Chiusura discrezionale (tesi invalidata, notizia) | PM | CHIUSURA al RM | — | Coperto: `02` regola 10 |
| C10 | Piano in scadenza (`closes_positions: true`, ultima seduta prima di `period_end`) | PM, finestre pre-chiusura (17:05 UE, 21:05 USA; crypto ultimo ciclo) | CHIUSURA «fine piano» di **ogni** posizione, in utile o in perdita → RM approva senza veto → CHIUDI all'ES; ingressi solo con il tempo di funzionare | — | Coperto: `00` §7 «Piani», §C11 2 e 5, `02` regola 11, §PM5, `03` CHIUSURA, `#47` |
| C11 | Posizione di un piano concluso ancora aperta (anche in perdita) | PM, RM (`plan_closing: true`, `plan.ended`) | nessuna aggiunta, inversione né stop allargato; a mercato aperto il RM può mandare il CHIUDI | rifiuta gli aumenti (`RISK_LIMIT_EXCEEDED`, `plan_grace`, «posizione in chiusura per fine piano»); annulla gli ingressi aperti | Coperto: §C11 3-4, §PM5, §RM3, `#48` |
| C12 | Chiusura automatica di fine piano | SMTrading; PM, RM, CEO (feed `plan.closed`) | nessuna azione; il PM rivaluta gli ingressi della strategia; il CEO lo riporta nella revisione del venerdì | chiude a mercato (reduce-only) ogni posizione rimasta, in utile o in perdita, alla prima seduta utile; aspetta l'apertura e una quotazione buona (mai vecchia o sospetta); Telegram agli umani | Coperto: §C11 3, 6-7, §CEO4, `#49` |

## D. Posizioni e strategie particolari

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| D1 | Stesso simbolo in due strategie dello stesso portafoglio | PM, RM | nessun secondo ingresso; l'uscita segue la strategia che l'ha aperta | posizione unica, prezzo medio | **Lacuna corretta**: `02` sentinella 8, `03` PROPOSTA 6 |
| D2 | Stesso simbolo in due portafogli (es. QQQ in una strategia di ‹A› e in una di ‹B›) | PM | posizioni separate, ognuna con la sua strategia e le sue uscite; concentrazione valutata dal RM per portafoglio | posizioni separate | **Lacuna corretta** (chiarito): `02` sentinella 8 |
| D3 | Posizione senza strategia (ordine manuale degli amministratori) | PM (09:05, 15:35), allarme di protezione | il PM decide tenere/chiudere e lo scrive; mai aumentarla; se senza stop, PROTEGGI con livelli del RM | allarme di protezione se manca lo stop | **Lacuna corretta**: `02` sentinella 8 |
| D4 | Strategia sospesa o revocata con posizioni aperte | CEO, PM | CEO: `details.open_positions` mantieni/chiudi + «STRATEGIA SOSPESA» al PM; PM: via alert, ANNULLA ingressi, posizioni secondo decisione | la strategia esce dai segnali | **Lacuna corretta**: `01` revisione 6, `02` «STRATEGIA SOSPESA», `#36` |
| D5 | Strategia modificata (nuovi parametri) | PM | nuovi parametri da subito, anche per l'uscita | — | **Lacuna corretta**: `02` sentinella 8 |
| D6 | `review_by` scaduto o vicino | CEO (venerdì), PM (checklist) | RIVEDI STRATEGIA al QR; niente nuovi ingressi finché non rivista; posizioni gestite | — | **Lacuna corretta**: `01` revisione 6, `07` RIVEDI, `#37` |
| D7 | Short tattico (su ogni portafoglio il cui profilo lo consente) | CEO, PM, RM | approvazione CEO, metà della dimensione di un acquisto, revisione ogni 2 settimane; con un carattere a rischio piccolo solo eccezionale | `SHORT_NOT_ALLOWED` se profilo o strumento non lo consentono | **Lacuna corretta** (non più limitato a un portafoglio): `00` §4, `01` regola 3, `02` regola 4, `03` regola 4 |

## E. Eventi societari

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| E1 | Utili entro 7 giorni su una posizione aperta | NA (AGENDA), PM | il PM decide e scrive: tenere, ridurre, chiudere, stop più stretto | — | **Lacuna corretta** (prima solo per le nuove proposte): `02` «AGENDA» 1 |
| E2 | Stacco del dividendo su una posizione aperta | NA (AGENDA), PM | come E1, considerando la discesa del prezzo allo stacco | accredito del dividendo (addebito se short) | **Lacuna corretta**: `02` «AGENDA» 1 |
| E3 | Frazionamento (split) di un titolo in portafoglio | RM (feed `split_orders_canceled`), allarme di protezione | PROTEGGI con stop e target sulla nuova scala (prezzo ÷ rapporto, qty da `get_positions`); ingressi annullati rivalutati dal PM | annulla **tutti** gli ordini del simbolo, nessun riscalamento | **Lacuna corretta**: `00` §7 «Posizioni» 5, `03` sentinella 4 e PROTEGGI, `02` feed |
| E4 | Notizia shock su una posizione aperta | NA → RM (eccezionale) o PM | RM decide nello stesso ciclo: CHIUDI, MODIFICA STOP o niente, copia PM | — | **Lacuna corretta** (il RM non aveva il passo): `03` «NOTIZIA urgente», `#42` |

## F. Rischio, limiti e blocchi

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| F1 | Perdita giornaliera ≥ 2/3 del limite | RM | ESCALATION informativa; il RM decide se fermare le aperture per la giornata (`risk_rule`) o chiedere un halt | — | Coperto: `03` regola 14 |
| F2 | Perdita giornaliera superata | RM, PM | nessuna apertura; il RM decide se ridurre | reduce-only fino a mezzanotte (dopo un fill o al controllo periodico), Telegram critico | Coperto; **lacuna corretta** su chi decide e su `current_eur` ≥ limite con stato ancora `active`: `00` §7 «Posizioni» 7, `03` sentinella 3 e regola 15 |
| F3 | Obiettivo `max_loss` vicino al limite / superato | RM e CEO (task di SMTrading) | RM: veto aperture + RIDUZIONE RISCHIO; CEO rivede e **decide la ripresa**; RM la registra e la comunica | 2 task «RISCHIO OBIETTIVO», Telegram | Coperto; **lacuna corretta** (ripresa non definita): `01`, `03`, `#40` |
| F4 | Obiettivo in ritardo / raggiunto / concluso / oltre il tetto | CEO (task), PM (AGENDA) | piano obiettivi, RICERCA STRATEGIA; tetto → ridurre rischio | task «OBIETTIVO» per «in ritardo», Telegram per gli altri | Coperto: `01` «Piano per gli obiettivi», `02` «AGENDA» 4 |
| F5 | Perdita della settimana rispetto ai limiti `max_loss` | RM, a ogni proposta | uno dei cinque controlli del RM | — | **Lacuna corretta**: `03` «Controlli che sono tuoi» |
| F6 | Limiti del profilo assenti (`null`: ordine e posizione massimi) | RM, PM | il RM decide rischio allo stop, peso, numero di posizioni, esposizione, perdita della settimana, proporzionati al carattere, e li motiva; soglie stabili con `risk_rule` | — | **Lacuna corretta**: `00` §2, `03` «Controlli che sono tuoi», `02` checklist 6, `08` regola 7b |
| F7 | Profilo cambiato dagli amministratori | RM (sentinella) | nuovi valori dal ciclo stesso; NOTIFICA informativa; posizioni oltre i nuovi limiti valutate dal RM | — | **Lacuna corretta**: `00` §2.5, `03` sentinella 3 e regola 19, `#39` |
| F8 | Kill switch acceso | tutti (`KILL_SWITCH_ACTIVE`) | solo chiusure e riduzioni; con ordini annullati: PROTEGGI per ogni posizione (l'OCO passa) | se scelto dall'admin annulla **tutti** gli ordini, uscite comprese; Telegram | **Lacuna corretta**: `00` §12, `03` PROTEGGI |
| F9 | Kill switch spento, halt revocato, reduce-only finito | PM (feed) | rivaluta gli ingressi di tutte le strategie al ciclo successivo | gli ingressi annullati non tornano | **Lacuna corretta**: `00` §7, `02` sentinella 1 e 8, `#43` |
| F10 | Halt attivato dal CEO (anche su richiesta RM) | CEO, ES, PM, RM | `set_trading_halt`, `risk_rule`, HALT all'ES; solo chiusure | annulla gli ingressi in attesa; Telegram | Coperto: `01` HALT, `#21`, `#21b` |
| F11 | Halt attivato dagli amministratori | CEO | non revocabile dal CEO: informa una volta, il team lavora entro l'halt | Telegram | **Lacuna corretta** (era «chiedilo»): `01` HALT, regola 6, «Escalation» |
| F12 | Reduce-only per anomalia del collegamento reale (senza scadenza) | RM | escalation informativa una volta; solo riduzioni | reduce-only finché l'admin non riprende | **Lacuna corretta**: `00` §12 `REDUCE_ONLY` |
| F13 | Portafoglio bloccato (`PORTFOLIO_LOCKED`) | RM, ES | nessun ordine possibile; escalation `critical` se ci sono posizioni | **non esegue nemmeno gli stop** | **Lacuna corretta**: `00` §7 e §12, `03` PROTEGGI |
| F14 | Portafoglio archiviato | tutti (`NOT_FOUND`) | rileggi `list_portfolios`, escalation | invisibile agli agenti | Coperto: `00` §12 |
| F15 | Margine in calo / short in perdita | RM | chiusure parziali (con riprotezione) | `margin_warning`, liquidazione | Coperto: `03` regola 17 |
| F16 | Portafoglio aggiunto o tolto dagli amministratori | tutti (`list_portfolios` a ogni ciclo) | il nuovo si tratta come gli altri dal primo ciclo (codice `code`, carattere `risk_profile.description`, limiti dal profilo; senza strategie il CEO avvia la RICERCA STRATEGIA); quello che non compare più non si usa | il codice si imposta nella pagina del portafoglio | **Lacuna corretta** (istruzioni senza elenco dei portafogli): `00` §2, appendice §C2 |

## G. Dati di mercato

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| G1 | Quotazione vecchia o assente su una posizione | RM (`stale`) | il RM decide se chiudere appena torna una quotazione valida; escalation al 2° ciclo | **gli stop non scattano** senza quotazione fresca | **Lacuna corretta**: `00` §7 «Posizioni» 4, `03` sentinella 4 |
| G2 | Fornitore dei dati fermo | tutti (`PROVIDER_UNAVAILABLE`, `stale_symbols`) | nessuna proposta su quel simbolo; escalation al 2° ciclo | aperture rifiutate nel portafoglio con valutazioni in cache | Coperto: `00` §11.3 e §12 |
| G3 | Prezzo esteso assente (`extended: null`) | RM, PM | nessuna conclusione sul gap | — | Coperto: `00` §6 |
| G4 | Cambio EUR/USD non disponibile per un acquisto in USD | PM, RM (`preview_order`) | `PROVIDER_UNAVAILABLE`: nessuna proposta in questo ciclo | — | Coperto: `00` §12 |
| G5 | `include_forming` non disponibile per uno strumento | PM | barre chiuse + SEGNALAZIONE STRUMENTO al CEO | — | Coperto: `02` sentinella 7, `01` |

## H. Calendario

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| H1 | Festività di un solo mercato (es. Thanksgiving USA) | PM, MA | `get_market_status`; nessuna barra di oggi per quel mercato | ordini in attesa fino all'apertura | Coperto: `00` §8.9, `05` escalation 3 |
| H2 | Tutte le borse dei portafogli chiuse | NA, PM | niente AGENDA né BUONGIORNO; crypto normale | controllo pre-apertura «Mercati chiusi oggi» | Coperto: `02`, `06`, `#1` |
| H3 | Chiusura anticipata o straordinaria decisa dall'admin | PM, RM | orari reali per finestra pre-chiusura e intraday | `"close"` e scadenze già sulla chiusura reale | **Lacuna corretta**: vedi A8, C8 |
| H4 | Weekend | PM, RM sentinelle crypto | solo crypto (vedi A5) | — | Coperto: `00` §6 |

## I. Infrastruttura

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| I1 | SMTrading non raggiungibile dagli agenti (es. DNS come il 16/11) | PM, RM | riprova una volta, chiude il task con l'errore; al 2° ciclo task al CEO + ESCALATION; al ritorno ciclo completo | stop e target lavorano comunque | **Lacuna corretta**: `00` §12, `01` task 6, `#38` |
| I2 | Paperclip fermo, cicli saltati | agente al ciclo successivo | recupero dal cursore, ciclo completo, controlli fissi mancati | sorveglianza dei passaggi avvisa gli umani | **Lacuna corretta**: `00` §6 «Ciclo saltato» |
| I3 | Riavvio di SMTrading | — | nessuna azione degli agenti | allarmi ripartono dopo 3 min; stop superati durante il fermo eseguiti al prezzo corrente | Coperto: `#2` |
| I4 | Sessione dell'agente azzerata (nuovi strumenti) | agente | cursore perso → `since`, ciclo completo | azzeramento automatico e Telegram | Coperto: `00` §6 «Contratto del cursore»; **lacuna corretta** (ciclo completo): `00` §6 |
| I5 | Task della Routine lasciato aperto | — | va sempre chiuso | cicli successivi saltati | Coperto: `00` §6 |
| I6 | Passaggio mancante (AGENDA, BUONGIORNO, PROTEGGI/RISCHIO OBIETTIVO senza risposta, ESEGUI aperto) | sorveglianza di SMTrading | gli agenti chiudono i task in tempo; il RM sollecita i suoi (B8) | Telegram «⚠ Passaggio mancante» agli umani | Coperto: `#2` |
| I7 | Budget mensile di Paperclip vicino alla soglia | — (gli agenti non lo vedono) | i cicli economici restano la regola | Telegram agli umani | Coperto: `00` §6 «Ciclo economico» |
| I8 | Posizione, liquidità o ordine aperto diversi tra SMTrading e il broker (riconciliazione, 2 controlli di fila) | SMTrading → RM (feed `broker.reconciliation_mismatch`) | RM verifica e decide (veto sul simbolo, PROTEGGI se manca uno stop al broker, ESCALATION); ES solo protezione su task del RM; PM nessuna proposta sul simbolo col veto; mai correzioni da soli | controllo ogni 15 min in seduta e dopo la chiusura USA, solo letture; Telegram agli umani una volta al giorno per voce; nessuna correzione automatica | Coperto (19/11): `00` §7, `03` sentinella 1, `04` regola 9, appendice §C19, `#50` |

## J. Processo e persone

| # | Scenario | Chi se ne accorge | Cosa deve succedere | SMTrading da sé | Copertura |
|---|---|---|---|---|---|
| J1 | Posizione senza stop (PROTEGGI) | SMTrading → RM | RM verifica e passa all'ES lo stesso titolo; casi: parziale, chiusura in coda, intraday, split, kill switch, blocco | allarme ogni 5 min, task al RM | Coperto; **lacuna corretta** per i casi particolari: `03` PROTEGGI |
| J2 | Disaccordo PM/RM dopo due veti | PM | «DISACCORDO» al CEO, che decide (veto confermato / `-v3`; può fissare una soglia del RM) | — | **Lacuna corretta** (finiva agli umani senza decisione): `00` §11.2, `01` task 5, `02` «Escalation», `#35` |
| J3 | Revisione e audit del venerdì | CEO 16:00 e 17:00, PA | AUDIT SETTIMANALE, revisione, NOTIFICA | riepilogo 18:30 | Coperto: `01`, `08`, `#28`, `#31` |
| J4 | Serve qualcosa che solo gli amministratori possono fare | CEO | RICHIESTA UMANI **informativa**; il team continua entro i limiti | — | **Lacuna corretta** (era domanda sì/no con scadenza): `00` §11, `01`, `09`, `#23` |
| J5 | Task creato da un umano (utente) | CEO o agente assegnato | esegue e risponde nel task | — | Coperto: `00` §6, `01` task 9 |
| J6 | Collegamento a un conto reale attivato o disattivato dagli amministratori | ES (`LIVE_NOT_ARMED`, `LIVE_CASH_LIMIT`, rifiuti del broker) | vedi B5-B6; nessun metodo degli agenti tocca il collegamento | Telegram per ogni ordine reale | Coperto: `00` §12 |
| J7 | Task creati dal RM (ESEGUI, CHIUDI, PROTEGGI, MODIFICA STOP) senza risposta | RM, sentinella | sollecito, poi task al CEO | — | **Lacuna corretta**: `03` sentinella 4 |
| J8 | Errore ripetuto o perdita evitabile (uscita mancata, passaggio non consegnato, dimensione sbagliata, regola non seguita) | chi lo trova, stesso ciclo; PA nell'audit | `log_decision` `lesson`; se ripete una lezione o una regola del team: «LEZIONE RIPETUTA» al CEO, che crea una `team_rule`, chiede RIVEDI STRATEGIA o registra «nessuna azione» con il motivo; gli agenti leggono le regole attive al primo ciclo della giornata e prima di proposte, approvazioni ed esecuzioni; umani informati il venerdì | le lezioni e le regole del team nei riepiloghi Telegram delle 18:00 e del venerdì | **Lacuna corretta**: `00` §9 «Lezioni e regole del team», `01` «Lezioni e regole del team» e revisione 6-8, `08` audit 3 e 8b, `02` checklist 0, `03` PROPOSTA 1, `04` «Prima di ogni task» 3, `07` RIVEDI STRATEGIA 1, `#44`-`#46` |

## Conteggio

- Scenari: **82** (A 10 · B 9 · C 12 · D 7 · E 4 · F 15 · G 5 · H 4 · I 8 · J 8; B5 raggruppa
  i 16 codici d'errore del rifiuto all'invio).
- Già coperti: **41** (C10-C12, i piani, aggiunti il 18/11 già coperti; I8, la riconciliazione, aggiunta il 19/11). Lacune corrette il 18/11: **41**, di cui 4 righe già coperte con un
  passo ambiguo (F2, F3, I4, J1).

## Decisioni affidate agli agenti

Nessuna decisione resta agli umani. Per ogni questione di politica trovata nel catalogo, chi
decide e su quale base (scritto nei file indicati):

| Questione | Chi decide | Base | File |
|---|---|---|---|
| Valori di rischio che il profilo non fissa (rischio allo stop, peso, numero di posizioni, esposizione totale, perdita della settimana) | **RM** | carattere del portafoglio, obiettivi, `daily_loss.limit_eur`; soglie motivate (`risk_rule`) | `00` §2, `03` |
| Limiti d'uso di una strategia (posizioni, rischio agli stop, peso) | **CEO** nell'approvazione, su proposta del **QR** | backtest | `01` regola 4, `07` template |
| Secondo ingresso sullo stesso simbolo nello stesso portafoglio | **PM** (regola: no) | una posizione per simbolo e portafoglio in SMTrading | `02` sentinella 8 |
| Posizione senza strategia | **PM** (tenere/chiudere), **RM** (livelli di protezione) | giudizio, limiti | `02` sentinella 8 |
| Posizioni di una strategia sospesa o revocata | **CEO** (`details.open_positions`) | revisione | `01` revisione 6 |
| Utili e dividendi su posizioni aperte | **PM** | stile, rischio dell'evento | `02` «AGENDA» 1 |
| Notizia eccezionale su posizione aperta | **RM** | limiti, perdita giornaliera | `03` «NOTIZIA urgente» |
| Rischio di gap fuori seduta, quotazioni vecchie vicino allo stop | **RM** | perdita stimata, limite giornaliero | `03` sentinella 4 |
| Riduzioni in reduce-only e dopo un cambio di profilo | **RM** | profilo, perdita giornaliera | `03` regole 15 e 19 |
| Ripresa delle aperture dopo un obiettivo `max_loss` | **CEO** (il RM registra) | stato dell'obiettivo, rischio | `01`, `03` |
| Disaccordo PM/RM dopo due veti | **CEO** | regole, limiti, criteri della strategia | `01` task 5 |
| Fine della validità di una proposta serale (ingresso `limit` + `valid_until`) | **PM** (il RM verifica) | gap all'apertura | `02` «Chiusura USA» 2 |
| Chiusura delle posizioni intraday | **PM**, riserva **RM** | orari reali di chiusura | `02` regola 5, `03` sentinella 7 |
| Short tattico con un carattere a rischio piccolo per operazione | **CEO** e **RM** (eccezionale) | carattere (`risk_profile.description`) | `00` §4 |
| Ripresa dopo `LIVE_NOT_ARMED` | **ES/RM** al giorno di borsa successivo | esito di `preview_order` e dell'invio | `00` §12 |
| Lezioni trasformate in regole del team, loro ritiro | **CEO** (chiunque registra le lezioni) | lezioni, audit, risultati; mai oltre profilo, kill switch, halt | `00` §9, `01`, `08` |

Limiti che **non** sono decisioni del team (gli agenti informano una volta e lavorano entro
il limite, senza attendere): kill switch, halt degli amministratori, blocco del portafoglio,
reduce-only del collegamento reale, profilo di rischio, capitale, collegamento al broker.
