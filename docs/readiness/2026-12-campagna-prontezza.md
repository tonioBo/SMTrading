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
| ven 27/11 dopo le 19 (USA mezza giornata) | Broker che non risponde (collegamento Alpaca di prova sospeso 10 min) | ordini in coda/rifiutati con avviso, nessun doppio invio, ripresa pulita, riconciliazione ok |
| dom 29/11 mattina | Ripristino backup + chiave | backup di sabato ripristinato in database usa-e-getta, segreti leggibili |
| mer 2/12 15:40 | Prova reale azioni su Alpaca (portafoglio temporaneo invisibile, 1 azione) | stop+obiettivo nativi, modifica, vendita, orari di mercato |
| sab 5/12 mattina | Blocco totale e ripresa (kill switch) a mercati chiusi | nuovi ingressi rifiutati, uscite consentite, ripresa, comportamento degli agenti al ciclo successivo |
| mer 9/12 sera | Prezzi sospetti (prezzo anomalo iniettato sul simulato) | la guardia scarta il prezzo, nessun ordine scattato a torto |
| sab 12/12 | Prova reale eToro con stop/obiettivo (nuova protezione) | l'altra uscita si annulla solo dopo l'esecuzione |
| dopo il 22/12 | Chiusura di fine piano | tutte le posizioni chiuse dagli agenti, residui chiusi da SMTrading alla prima seduta valida |

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
