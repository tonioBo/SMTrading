# Campagna dicembre 2026 — prontezza al trading vero

Campagna: 23/11/2026 – 22/12/2026 · Portafoglio Alpaca (AGG, conto di prova Alpaca) ·
Portafoglio eToro (PRU, conto demo eToro) · Portafoglio Moderato (MOD, simulato).
Scopo (Matteo, 21/11/2026): usare il mese per arrivare al denaro vero senza sorprese,
analizzando tutto, dal comportamento degli agenti al portale SMTrading.
Documento solo per il proprietario: non è nelle istruzioni degli agenti.

## 1. Registro delle anomalie

Ogni problema trovato (portale, broker, dati, agenti): data, cosa, causa, correzione, se si è ripetuto.
Gravità: **A** = avrebbe perso o esposto denaro / dati sbagliati · **B** = funzione non disponibile o
errata senza danno diretto · **C** = fastidio, rumore, estetica.

| # | Data | Area | Gravità | Cosa | Causa | Correzione | Ripetuto? |
|---|---|---|---|---|---|---|---|
| 1 | 21/11 | eToro | A | Nessun acquisto possibile: ogni strumento risultava «non negoziabile» | Risposta di idoneità eToro con chiave `eligibilities` non letta; test con broker finto nel vecchio formato | d99d1e4, finto allineato al formato vero | — |
| 2 | 21/11 | eToro | A | Nessuna vendita registrata: chiusura eseguita da eToro ma «in attesa» per sempre; riga di disallineamento visibile agli agenti ogni 5 min | Conferma di chiusura con quantità e prezzo in `positions[]` | 72aa21a; righe visibili cancellate | — |
| 3 | 21/11 | eToro | B | Commissione di vendita registrata 1,08 € invece di ~0,90 $ | Commissione eToro non letta sulle chiusure | e13c282 (lordo − incassato) | — |
| 4 | 21/11 | Ordini eToro | A | Su un conto condiviso una vendita «di troppo» poteva chiudere la posizione di un altro portafoglio | Nessun controllo della posizione all'invio | p33 (B): vendite limitate alla posizione | — |
| 5 | 21/11 | Ordini eToro | A | Stop/obiettivo: l'altro veniva annullato prima dell'esecuzione → posizione scoperta se eToro rifiuta | Annullo al momento dello scatto | p33 (A): annullo solo dopo l'esecuzione | — |
| 6 | 21/11 | Ordini eToro | B | Limite scattato e inviato in ritardo poteva eseguire a qualsiasi prezzo | Nessun ricontrollo del prezzo all'invio | p33 (C) | — |
| 7 | 21/11 | Chiusura piano | B | Possibile doppia chiusura con un'uscita già in corso | Controllo «uscita in corso» incompleto | p33 (D) | — |
| 8 | 21/11 | Mercati (portale) | B | Limite Yahoo del portale poteva togliere Yahoo al trading; calendario senza date per 12 h; avvisi su prezzi vecchi | Blocco quota condiviso; cache dei fallimenti | p32 | — |
| 9 | 21/11 | Dati | C | A2A.MI senza prezzo in un momento di guasto Yahoo; 4 cripto non trovate su Yahoo | Una sola fonte per i titoli UE; simboli Yahoo diversi | p32: eToro come riserva UE, simboli Yahoo | — |
| 10 | 21/11 | Flusso Alpaca | C | 6 rifiuti «troppe connessioni» | Sessione precedente chiusa male contata da Alpaca | p32: un solo proprietario del flusso, attesa lunga, prezzi a richiesta nel frattempo | — |
| 11 | 21/11 | Alpaca cripto | A | Commissione cripto presa da Alpaca **in moneta** sugli acquisti (0,25%): SMTrading accredita la quantità lorda → posizione fantasma, uscita completa rifiutata, righe di disallineamento visibili agli agenti | Commissione cripto non letta (Alpaca non la mette nell'ordine) | b5ff62c (quantità netta, commissione registrata, uscite adattate al saldo); verificato sul conto reale 21/11 22:35 | — |
| 12 | 21/11 | Alpaca cripto | A? | Stop semplice non supportato da Alpaca sulle cripto: rifiutato (e un bracket con stop potrebbe lasciare la posizione senza protezione) | Solo market/limit/stop-limit sulle cripto | b5ff62c: stop e uscite cripto tenuti da SMTrading e inviati a mercato; verificato | — |
| 13 | 21/11 | Commissioni | B | Portafogli della campagna senza regola propria → «Default globale» 1 € + 0,1% anche dove il broker non addebita | Regole non impostate alla creazione | applicati i listini dei broker (Alpaca, eToro; Moderato = Alpaca) | — |
| 14 | 21/11 | Protezione | B | L'allarme «posizione senza stop» guarda anche i portafogli senza agenti (apre un task al Risk Manager) | Ambito dell'allarme | da valutare: escludere i portafogli senza accesso agenti | — |
| 15 | 23/11 | Agenti | C | Avvio campagna: passaggi di consegna creati due volte (3 «STRATEGIA APPROVATA» doppi, 1 «ALLOCAZIONE · MOD» doppio rimasto in attesa); ~8,7 $ per l'avvio | da capire (CEO e Quant creano entrambi il passaggio?) | da osservare alla pagella | — |
| 16 | 23/11 | Agenti (RM, Sonnet) | C | Ciclo delle 15:00: id del Portafoglio Alpaca trascritto male 4 volte (INVALID_ORDER), corretto da solo in 14 s | il modello ricopia a memoria l'id lungo | da valutare: accettare anche il codice del portafoglio (AGG) negli strumenti MCP | — |
| 17 | 23/11 | Ordini Alpaca | A | Bracket su azioni: l'obiettivo rifiutato («quantità insufficiente»), Alpaca blocca le azioni per il primo ordine di vendita | due vendite separate per la stessa quantità | 128fd8c: uscite tenute da SMTrading, una sola inviata; MSFT ripristinato | — |
| 18 | 23/11 | Flusso Alpaca | B | Disconnessione ogni minuto dall'apertura USA, quotazioni in ritardo | lettura più lenta dell'arrivo delle quotazioni | f6eab58: lettura continua, ultimo prezzo per titolo | — |
| 19 | 24/11 | Stop | A | Stop MSFT 478,33 scattato all'apertura su una quotazione con forbice del 10% (bid 470,77 / ask 521,51): venduto a 490,47, prezzo mai sotto lo stop; costo ~55 $ | il denaro di una forbice larga trattato come prezzo | 7d54bd9: con forbice >1% decide l'ultimo scambio, senza scambio si aspetta | — |
| 20 | 27/11 | Flusso Alpaca | B | Con le regole di prova attive Alpaca ha rifiutato il flusso (405) e SMTrading l'ha ridotto a 10 titoli fino al riavvio: 11 titoli della campagna a lettura periodica | Limite del piano gratuito = 30 abbonamenti (prezzi + scambi per titolo), contato come 30 titoli | 8e141d3 (limite in abbonamenti, riduzione non permanente) | — |
| 21 | 27/11 | Prezzi Alpaca | A | Forbice anomala del 10% (JPM) dal flusso gratuito usata come riferimento: rifiuti di stop/obiettivo, scarti falsi; in simulazione poteva dare esecuzioni a prezzi irreali | Prezzi di acquisto/vendita grezzi usati in più punti | f1fb486, 571d3a4, 6f99a12 (forbice > 1% → ultimo scambio dentro la forbice, solo Alpaca) | — |
| 22 | 27/11 | Regole automatiche | B | Stop/obiettivo calcolati su un prezzo e controllati su un altro; errore interno nelle uscite (riprova dopo 5 s); primo controllo dei segnali senza scatti | Due fonti di prezzo; lettura dopo transazione annullata; nessuno stato iniziale | 1fe7bd9, a41b2d1, 31f48a7 | — |
| 23 | 27/11 | Telegram | C | «Ordine eseguito» con la quantità dell'ultima parte invece del totale (ordine eseguito in 3 parti) | Messaggio costruito dall'ultima esecuzione | 103b8e2 (in linea 27/11 20:45) | — |
| 24 | 27/11 | Broker di prova | B | Esercitazione broker irraggiungibile: nessun avviso dedicato per un collegamento di prova che non risponde (solo il conteggio errori della sentinella dopo ~4 min); errori ripetuti a ogni giro; un ordine a mercato bloccato parte quando il broker torna, anche 10 min dopo | Stato del collegamento aggiornato in silenzio ogni 5 min; nessuna attesa dopo un errore di rete; finestra di invio non applicata ad Alpaca | 11caafb, a93bc68, 3699233, 61dcc8e (in linea 27/11 20:45): avviso dopo ~2 min, ordini a mercato bloccati > 5 min rifiutati con avviso, errori di rete senza ripetizioni | — |
| 25 | 24–25/11 | Commissioni Alpaca | B | 0,07 $ di spese addebitate da Alpaca dopo la vendita MSFT, registrate 0 | Spese regolamentari sulle vendite postate dal broker il giorno dopo come movimento separato, non lette | in correzione | — |
| 26 | 27/11 | Agenti (Notifier) | B | Notifica della revisione settimanale mai partita: task bloccato dalle 16:01 | Alle 16:01 Paperclip ha segnalato il superamento della soglia del budget della società e ha fermato l'esecuzione in corso; il task è rimasto bloccato | sbloccato 27/11 22:54, notifica consegnata; la causa della soglia scattata al 78% resta da capire | — |
| 27 | 23–27/11 | Costi agenti | B | Portfolio Manager 52,52 $ e Risk Manager 45,07 $ nel mese, oltre il tetto di 30 $ a testa, senza pausa né avviso (società 157,30 $ su 200 $) | Tetti per agente non applicati da Paperclip (c'è solo il tetto della società, 200 $, che ferma tutti) | 27/11 Matteo: tetti per agente lasciati così, solo avviso su Telegram quando superati o vicino ai 200 $ | — |
| 28 | 27/11 | Telegram (riepiloghi) | C | I riepiloghi giornaliero e settimanale contano anche ordini e posizioni dei portafogli di prova nascosti, mentre il valore totale li esclude | Conteggi senza il filtro dei portafogli di prova | f088560 (da mettere in linea) | — |
| 29 | 23/11 | Sentinella | C | 2 falsi allarmi critici: «firmata da Portfolio Manager ma scritta da PM» (abbreviazione) e «4 identità in un minuto» per esecuzioni programmate alla stessa ora | Confronto sul nome per esteso; soglia che ignora le esecuzioni programmate | già corretto il 23/11 (3ac4085, b70b642), nessun falso allarme da allora | — |
| 30 | 23–27/11 | Strumenti agenti | C | Lettura indicatori rifiutata 8 volte con codice «ordine non valido» per parametri scritti in forma piatta; l'agente si corregge da solo | Forma `params` obbligatoria; codice d'errore fuorviante | proposta in attesa (come riga 16) | — |

## 2. Pagella settimanale (venerdì sera, Telegram)

Per ciascun portafoglio e in totale:
- **Esecuzioni**: ordini, eseguiti, rifiutati (motivi), scarto prezzo atteso → eseguito (media e peggiore), tempo scatto → esecuzione, commissioni registrate vs broker.
- **Allineamento broker**: esiti della riconciliazione (ok / avvisi / disallineamenti), liquidità e posizioni.
- **Protezione**: posizioni rimaste senza stop (quante, per quanto tempo), allarmi di protezione.
- **Agenti**: cicli previsti vs svolti, operazioni senza segnale, regole/limiti violati o sfiorati, errori degli strumenti, escalation al CEO, lezioni scritte, costo per agente e per modello.
- **Portale/sistema**: errori nei registri, servizi giù, fonti dati degradate o esaurite, backup.
- **Obiettivo**: andamento «Mensile +5%».
- **Anomalie nuove** della settimana (dal registro) e stato di quelle aperte.

## 3. Esercitazioni programmate

In orari tranquilli, una a settimana; esito annotato qui e nella pagella.

| Data | Esercitazione | Cosa si verifica |
|---|---|---|
| sab 21/11 sera | Prova reale cripto su Alpaca (portafoglio temporaneo invisibile) | **fatta**: limite a riposo e annullo ok; acquisto eseguito in 3,5 s; trovati #11 #12; uscita e pulizia ok, conto vuoto |
| ven 27/11 dopo le 19 (USA mezza giornata) | Broker che non risponde (collegamento Alpaca di prova sospeso 10 min) | ordini in coda/rifiutati con avviso, nessun doppio invio, ripresa pulita, riconciliazione ok <br>**Fatta 27/11 19:26–19:36:** broker di prova irraggiungibile 10 min (in modo reversibile); ordine BTC di prova rimasto in attesa, **inviato una sola volta** al ritorno ed eseguito; nessun doppio; ripresa pulita; riconciliazione senza differenze. Difetti: avviso dedicato mancante, errori ripetuti, ordine a mercato in ritardo (registro, riga 24). |
| dom 29/11 mattina | Ripristino backup + chiave | backup di sabato ripristinato in database usa-e-getta, segreti leggibili |
| mer 2/12 15:40 | Prova reale azioni su Alpaca (portafoglio temporaneo invisibile, 1 azione) | stop+obiettivo nativi, modifica, vendita, orari di mercato |
| sab 5/12 mattina | Blocco totale e ripresa (kill switch) a mercati chiusi | nuovi ingressi rifiutati, uscite consentite, ripresa, comportamento degli agenti al ciclo successivo |
| mer 9/12 sera | Prezzi sospetti (prezzo anomalo iniettato sul simulato) | la guardia scarta il prezzo, nessun ordine scattato a torto |
| sab 12/12 | Prova reale eToro con stop/obiettivo (nuova protezione) | l'altra uscita si annulla solo dopo l'esecuzione |
| dopo il 22/12 | Chiusura di fine piano | tutte le posizioni chiuse dagli agenti, residui chiusi da SMTrading alla prima seduta valida |
| quando arrivano le chiavi del nuovo conto di prova Alpaca da 30.000 $ (seduta piena) | Prova reale del lavoro in giornata nei due modi (due portafogli di prova nascosti sullo stesso collegamento nuovo: sveglia su strategia 15m; regole automatiche su 1-2 titoli e 1-2 cripto) | scatti e messaggi alle chiusure di barra; tempi scatto→esecuzione, scarti, commissioni e risultati delle regole; uscite a tempo e di fine seduta; freni; rifiuto del manuale sul simbolo di una regola; tetto di giornata, leva 2 e potere d'acquisto del broker; rete di fine seduta; nessun disturbo alla campagna; pulizia con conto vuoto (manuale: `2026-12-intraday-prova-alpaca.md`) <br>**Fatta in parte 27/11 (mezza giornata USA + cripto):** 42 operazioni automatiche su 9 simboli, scatto→invio ~0,25 s, esecuzione 0,3–2 s, netto −12,71 € su 10.000 € finti; freni per regola ok (tutte fermate dopo 3 perdite); tetto di giornata ok; ordini oltre il potere d'acquisto rifiutati; rete di fine seduta ok (PEP chiusa 18:50, nulla dopo le 19); 1 sveglia modo A (AAPL 15m, 70 s dopo la chiusura barra). Difetti trovati e corretti (in linea 27/11 sera): limite del flusso prezzi Alpaca contato in abbonamenti, prezzo con forbice anomala del flusso gratuito, riferimento di stop/obiettivo delle regole, lettura dopo transazione annullata, primo controllo dei segnali che non scattava. Da rifare in seduta piena: uscite di fine seduta delle regole, leva 2 come limite che scatta, sveglia dell'agente. |

## 4. Lista finale sì/no (a fine campagna)

Ogni criterio: soglia, esito misurato, sì/no. Si passa al reale solo con tutti «sì» (o deroga
scritta del proprietario).

| Criterio | Soglia |
|---|---|
| Anomalie di gravità A aperte | 0 |
| Anomalie A comparse nelle ultime 2 settimane | 0 |
| Posizioni senza stop oltre 5 minuti | 0 |
| Riconciliazione | nessun disallineamento non spiegato; avvisi chiariti entro il giorno |
| Ordini rifiutati per errore di SMTrading (non del mercato) | 0 nelle ultime 2 settimane |
| Commissioni registrate vs broker | scarto < 1% |
| Scarto medio prezzo atteso → eseguito | entro lo spread tipico; nessun caso > 1% su titoli liquidi |
| Cicli degli agenti svolti | ≥ 98% |
| Operazioni senza segnale / limiti del profilo violati | 0 |
| Esercitazioni | tutte superate |
| Servizi e dati | nessun fermo > 15 min in orario di mercato non segnalato |
| Backup | ripristino provato riuscito |
| Costi agenti | entro il budget mensile |
| Obiettivo | non è un criterio di sicurezza: si riporta il risultato |

## 5. Idee per dopo la campagna

- **Lavoro in giornata (intraday) — prossimo sviluppo** — 26/11, richiesta di Matteo. Oggi i segnali sono
  quasi tutti su barre giornaliere e gli agenti guardano i portafogli circa ogni ora. Cosa serve:
  1. prezzi davvero in tempo reale (il flusso gratuito Alpaca vede una sola borsa e max 30 titoli; UE da
     Yahoo/eToro può essere in ritardo) → scegliere una fonte a pagamento per i mercati trattati in giornata;
  2. SMTrading sorveglia i segnali in continuo (come già fa con stop e obiettivi) e sveglia l'agente con un
     compito solo quando un segnale scatta, invece di cicli più frequenti (costo e lentezza);
  3. strategie su barre brevi (5m/15m/1h, già supportate) con storico sufficiente (Alpaca o fornitore) e
     costi veri del broker nelle prove;
  4. regole di giornata: limite USA per conti < 25.000 $ (max 3 operazioni aperte e chiuse in giornata
     ogni 5 giorni, contate da SMTrading), chiusura delle posizioni di giornata prima della fine seduta
     (chi la fa, sempre in orario di borsa), taratura di ordini/ora e perdita giornaliera nei profili;
  5. stile «in giornata» nei profili dei portafogli (letto da SMTrading, istruzioni generiche) e stima
     del costo degli agenti.
  Proposta: partire dopo il go/no-go del 23/12 con un solo portafoglio sul conto di prova Alpaca.
- **Opzioni (Alpaca)** — 24/11, richiesta di Matteo di segnarla per il futuro: opzioni su azioni ed ETF anche in orari estesi (dal 4/12/2026 sul reale, già provabili sul conto di prova). Oggi SMTrading non ha opzioni (catalogo, prezzi, ordini, rischio, strumenti degli agenti). Da valutare come progetto separato solo dopo il passaggio al denaro vero sulle azioni.
