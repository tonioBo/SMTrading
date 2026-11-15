# SMTrading — Piano 9, Task 18: controllo pre-apertura e obiettivo settimanale

Approvato dall'utente il 15/11/2026 sera. Nessuna migrazione: le impostazioni stanno in `app_settings`.

## A) Controllo pre-apertura (`telegram/preopen.py`)

- **Quando:** dal lunedì al venerdì alle 08:45 ora locale (`SMT_TZ`, Europe/Rome). Lo si accende/spegne e se ne cambia l'orario da Impostazioni → «Messaggi Telegram». Chiave `preopen_check` = `{on, at}`, predefinito acceso alle 08:45. L'impostazione sta in `telegram/preopen_settings.py`, così l'API admin non importa il lato worker.
- **Chi lo esegue:** un ciclo del worker (`PreopenCheck`, sullo stesso schema di `SummaryScheduler`). È idempotente per data locale: `job_runs`, job `telegram.preopen`, rivendicato nella stessa transazione della riga di outbox.
- **Cosa controlla:**
  1. le quotazioni, tramite il router dei dati di mercato, dei simboli con posizioni aperte e poi degli `allowed_symbols` dei profili di rischio, al massimo 20: devono essere disponibili e più recenti di 4 giorni;
  2. NYSE, NASDAQ, MTA e XETRA oggi: aperti, chiusi o con chiusura anticipata (quelle manuali sono indicate);
  3. il battito del worker (al massimo 60 s), Redis, il database, e i lock advisory di BrokerSync e LiveMonitor (segno che i loro cicli girano);
  4. kill switch spento, nessun blocco aperture attivo, nessun portafoglio bloccato o in sola riduzione (se ce ne sono, li elenca: al massimo 5, poi «… e altri K»);
  5. i collegamenti broker attivi: quelli in errore, con il motivo breve, e quelli reali armati o in prova a secco i cui dati del conto mancano o sono fermi da più di 30 minuti.
- **Messaggio:** uno solo, categoria `system`, scritto direttamente in outbox e mai nel feed attività che leggono gli agenti. Può essere:
  - «✅ Pronti per l'apertura — data» con 3-6 righe («Sistema: servizi attivi; …»);
  - «⚠️ Controllo pre-apertura: N problemi — data» con l'elenco;
  - «📅 Mercati chiusi oggi — data» quando i quattro mercati sono tutti chiusi.
  Il sabato e la domenica non parte nulla. Il testo passa sempre da `render.split_message` e ogni parte va in outbox nella stessa transazione, quindi non supera mai il limite di Telegram.
- **Telegram non pronto:** non si rivendica nulla e il messaggio parte appena Telegram è pronto, entro 2 ore dall'orario impostato. Dopo, il giorno è segnato come fatto e saltato, con un WARNING.
- **R9:** prima le letture dal database in una sessione breve, poi Redis e quotazioni, senza transazioni aperte.
- **Paperclip:** solo se il lettore è attivo. È un problema (⚠️) se Paperclip non risponde da almeno 3 letture di fila (con il motivo breve dell'ultimo errore) o se l'isolamento non è `ok` (regole cambiate, non verificato, non attivo). Con il lettore spento non si aggiunge nulla.

## B) Obiettivo settimanale (`telegram/weekly_goal.py`)

- **Impostazione** «Obiettivo settimanale»: chiave `weekly_goal` = `{on, pct}`, predefinita accesa al +2,0%. La percentuale va da 0 (escluso) a 100, con al massimo 2 decimali. La modifica solo l'admin; il viewer la vede in sola lettura, perché `PUT /api/settings/app` passa da `require_access` ed è già nel catalogo del registro attività come `system.app_settings`.
- **Riga** nel riepilogo delle 18:00 e in quello settimanale, sotto i conteggi: «🎯 Obiettivo settimana +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 € (+0,42%)».
- **Calcolo:** somma dell'equity in EUR dei portafogli non archiviati.
  - Inizio: lo snapshot `day_open` del lunedì, altrimenti il primo snapshot della settimana.
  - Valore attuale: l'ultimo snapshot.
  - Un portafoglio senza snapshot nella settimana resta fuori da entrambe le somme. Senza dati la riga dice «dati della settimana non ancora disponibili».
  - Depositi e prelievi fatti in settimana entrano nella percentuale: per ora non vengono scorporati.
- **Visibilità:** la riga va solo su Telegram e non arriva mai agli agenti.
