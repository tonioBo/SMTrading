# SMTrading — Notifiche Telegram e agente Notificatore (design)

Data: 2026-11-13 · Stato: bozza da approvare

## Perché
Chi gestisce la piattaforma vuole vedere su Telegram tutto ciò che gli agenti Paperclip decidono e fanno. Gli eventi critici devono arrivare anche se Paperclip è fermo.

## Obiettivi
- Un gruppo Telegram (bot + ID gruppo) riceve:
  - **direttamente da SMTrading**: eventi critici e ordini eseguiti o rifiutati;
  - **dall'agente Notificatore**: decisioni, riassunti e tutto il resto.
- Le decisioni prese in Paperclip vengono registrate in SMTrading con il metodo MCP `log_decision`, disponibile per tutti gli agenti. Restano nello storico, visibile dalla web app.
- Solo il Notificatore può inviare messaggi Telegram tramite MCP (nuovo permesso `notify`), con un limite di frequenza.

## Non-obiettivi
- Comandi da Telegram verso SMTrading: niente bot interattivo, niente ordini via chat.
- Chat diverse per tipo di evento, o chat private.
- SMTrading non legge Paperclip.

## Anti-obiettivi
- Un token del bot in chiaro in log, errori o risposte API.
- Il Notificatore che inonda il gruppo (rate limit).
- Un evento critico perso perché Telegram non risponde. Va gestito con un nuovo tentativo e una coda persistente.

## Approcci considerati
1. **Ibrido** (scelto): SMTrading invia da sé gli eventi critici e quelli eseguiti; l'agente invia decisioni e riassunti. È affidabile per il rischio e lascia la flessibilità all'agente.
2. Solo l'agente: più semplice, ma se Paperclip è fermo non arriva nulla.
3. Solo SMTrading: nessun riassunto ragionato; le decisioni arriverebbero solo come righe grezze.

## Design

### Dati (migrazione nuova)
- `decisions`: id, agent_id, portfolio_id?, kind, summary (≤ 500), details JSON, symbols[], created_at. Append-only.
  - kind: `proposal`, `approval`, `veto`, `allocation`, `strategy`, `risk_rule`, `other`.
- `telegram_outbox`: id, source (`system`|`agent:<id>`), text, status (`pending`/`sent`/`failed`), attempts, last_error (redatto), created_at, sent_at.
- La configurazione Telegram (`bot_token` cifrato con `common.crypto`, `chat_id`, attivo sì/no) vive nella tabella `settings` o in un connettore dedicato `telegram`, con il mascheramento dei segreti già esistente.

### Feed attività (per il Notificatore)
Il metodo MCP `get_activity_feed(after_id?, limit≤200)` restituisce in ordine crescente con un cursore (`next_after_id`):
- notifiche esistenti;
- ordini: creati, eseguiti, rifiutati, annullati, con il nome dell'agente;
- decisioni;
- azioni admin: kill switch, reset, depositi.

Ogni elemento contiene `{id, ts, type, agent, portfolio, summary, data}`. Il feed è limitato ai portafogli su cui l'agente ha almeno il permesso `read`.

### Metodi MCP
- `log_decision(kind, summary, portfolio?, symbols?, details?)`: per tutti gli agenti con accesso al portafoglio.
- `get_activity_feed(after_id?, limit?)`.
- `send_telegram(text, level=info|warning|critical)`:
  - solo con il permesso `notify`;
  - massimo 20 messaggi al minuto e 4096 caratteri;
  - il testo viene inviato come testo semplice, senza parse_mode, quindi niente iniezione di markup;
  - il messaggio passa dalla `telegram_outbox`.
- `get_decisions(portfolio?, since?, limit?)`.

### Invio diretto (worker)
- Il `TelegramNotifier` nel worker si iscrive a eventi e notifiche e accoda in `telegram_outbox`:
  - liquidazione;
  - kill switch attivato o disattivato;
  - perdita giornaliera;
  - avviso di margine;
  - broker: ordine sconosciuto o riconciliazione con differenze;
  - ordine eseguito o rifiutato.
- Un sender svuota la outbox con un nuovo tentativo esponenziale (al massimo 10) e rispetta il `retry_after` di Telegram (429).
- I messaggi sono in italiano, con prefisso per livello (🔴 critico, 🟠 avviso, 🟢 eseguito).

### Web app — sezione «Notifiche»
- **Configurazione:** token del bot (mascherato), ID del gruppo, interruttore attivo, pulsante «Invia messaggio di prova».
- **Guida rapida:** come creare il bot con @BotFather e come ottenere l'ID del gruppo.
- **Modifica comoda** (richiesta utente 2026-11-13):
  - Il token del bot si può sostituire senza mai rivederlo. Il campo mostra solo `••••` e il nome del bot letto con `getMe`, per esempio `@Smtradingnotifiche_bot`.
  - L'ID del gruppo è modificabile, con accanto il titolo del gruppo letto con `getChat`.
  - Pulsante **«Rileva gruppi»**: lato server chiama `getUpdates` e mostra le chat recenti in cui il bot ha ricevuto messaggi (ID, tipo, titolo). Con un clic si sceglie il gruppo. Il testo spiega di scrivere `/start@<bot>` nel gruppo prima di premerlo.
  - Prima di salvare un nuovo token o un nuovo ID viene verificato il collegamento (`getMe` e `getChat`). Gli errori di Telegram compaiono in italiano, senza il token.
  - Interruttore per mettere in pausa tutti gli invii. Gli eventi restano in coda con stato `paused` e non vengono persi.
  - Ogni modifica finisce nel log di audit amministrativo.
- **Ultimi messaggi:** dalla outbox, con stato, errori e un pulsante «Riprova».
- **Decisioni:** elenco filtrabile per agente, portafoglio e tipo.

### Agenti
- Nuovo agente SMTrading «Notifier» con permesso `read` su Paperclip e `notify`. Il token viene salvato nel file privato, come per gli altri agenti.
- Il Notificatore lavora **su richiesta**, senza polling periodico (decisione dell'utente del 2026-11-13):
  - gli agenti che decidono registrano la decisione con `log_decision` e coinvolgono il Notifier in Paperclip, con un task o una menzione;
  - il Notifier invia il messaggio con `send_telegram`;
  - sono programmati solo il riassunto giornaliero (18:00 Europe/Rome) e quello settimanale (venerdì), costruiti con `get_activity_feed`;
  - il feed marca con `already_sent: true` gli eventi che SMTrading ha già inviato.
- Il prompt suggerito per il Notifier va in `docs/paperclip-mcp.md`.

## Lasciato al builder
- Il formato esatto dei messaggi.
- I nomi di colonne e indici.
- Se la configurazione vive in `settings` o in un connettore.
- Il numero della migrazione: la successiva libera dopo il piano 6.

## Dove si inserisce
Piano 7, dopo la chiusura del piano 6, su un ramo nuovo impilato `feat/plan-07-telegram`, con lo stesso processo di revisione.
