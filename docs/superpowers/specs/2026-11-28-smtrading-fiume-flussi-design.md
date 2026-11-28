# Fiume dei flussi — progetto

Data: 28/11/2026 · Richiesta di Matteo · Stato: da approvare

## Perché

Matteo vuole **capire come funziona** SMTrading guardandolo lavorare: chi parla con chi (agenti, Paperclip, MCP,
motore ordini, BrokerSync, broker, prezzi, Neurograph, database, Telegram) e quanto traffico passa su ogni
collegamento, con i flussi veri in tempo (quasi) reale. Scopo scelto: «capire come funziona», non sala di
controllo per gli allarmi. Deve essere «molto bello da vedere».

## Obiettivi

1. Una pagina nuova del portale, **«Fiume dei flussi»**, con lo stile della bozza approvata
   (`.superpowers/brainstorm/…/content/fiume-pro.html`): sei colonne da sinistra a destra (agenti ·
   orchestrazione · porta MCP · motore e dati · uscite · broker), correnti larghe quanto il traffico, scie di
   particelle nel verso prevalente, una cometa per ogni passaggio con il nome dello strumento, onda sul nodo che
   riceve (rossa se errore), schede al passaggio del mouse, clic su un nodo per fissare il suo fiume, andamento
   degli ultimi 2 minuti, diario delle chiamate in diretta, contatori (chiamate/min, ordini, errori, tempo medio),
   comandi pausa e velocità di riproduzione, finestra della larghezza 1/5/15 min, «segui un ordine».
2. **Solo dati veri** (deciso da Matteo: niente modalità dimostrazione). Quando non c'è traffico la pagina lo
   dice («nessuna chiamata negli ultimi N minuti») e mostra le correnti con la larghezza dell'ultima finestra.
3. **Lettura ogni ~2 secondi** dai registri esistenti (deciso da Matteo, strada A): nessuna modifica al
   percorso delle chiamate degli agenti, al motore, a BrokerSync; **catalogo MCP invariato**.

## Non obiettivi

- Nessun allarme, nessuna azione dalla pagina (sola lettura).
- Nessuna spinta in tempo reale nuova (pubblicazione di eventi dall'MCP): esclusa per la campagna.
- Nessun dato di portafogli di prova nascosti (senza agenti) né dei portafogli archiviati: la pagina mostra
  solo il traffico che conta per la campagna e il portale (stesso filtro dei riepiloghi, `total_portfolio_ids`
  dove si parla di portafogli; le chiamate MCP di agenti sono sempre della campagna).
- Niente parametri sensibili: delle chiamate MCP si mostrano solo strumento, agente, esito, durata e simbolo
  (se presente); mai gli altri argomenti.

## Anti-obiettivi (sarebbe un fallimento anche se funziona)

- Rallentare SMTrading o il database durante la campagna (letture pesanti ogni 2 s).
- Inventare passaggi che non sono avvenuti: ogni cometa deve corrispondere a un fatto registrato.
- Mostrare agli agenti qualcosa di nuovo (la pagina è solo del portale, per gli amministratori).

## Vincoli

- Campagna in corso fino al 23/12: messa in linea **solo con gli agenti Paperclip fermi**; backup prima.
- Catalogo MCP congelato (`test_mcp_catalog_frozen`, `test_mcp_unchanged_after_routes`): nessun cambio.
- Rotte nuove: GET solo amministratori → vanno nell'elenco `ADMIN_ONLY_READS` (test_admin_users) e
  nelle azioni registrate del portale come le altre rotte di lettura (stesso schema delle rotte esistenti).
- Codice in inglese, testi del portale in italiano semplice; web React 18 + TanStack Query + Vite (esistenti);
  nessuna libreria grafica nuova necessaria (Canvas 2D come nella bozza).
- Il carico: una query ogni 2 s per ogni pagina aperta, con indici esistenti o un indice nuovo leggero
  (migrazione solo se serve, a costo zero sulle scritture calde: valutare `mcp_audit_log(created_at)`).

## Le strade considerate

| | Come | Pro | Contro |
|---|---|---|---|
| **A · lettura ogni 2 s (scelta)** | Il portale chiede «cosa è successo dopo il cursore X»; il backend legge i registri esistenti e ricostruisce i passaggi | Zero rischio per il percorso degli agenti; catalogo MCP invariato | ~2 s di ritardo |
| B · spinta in diretta | L'MCP pubblica ogni chiamata sul canale `events` già inoltrato da `/ws` | Immediato | Tocca ogni chiamata degli agenti in campagna |
| C · A ora, B dopo il 23/12 | Come A, poi B | Futuro immediato | Due lavori |

Scelta di Matteo: **A**.

## Come funziona (deciso)

### Il modello dei passaggi

La pagina disegna nodi e collegamenti fissi (stesso elenco della bozza: 9 agenti, Paperclip, MCP SMTrading,
Regole automatiche, Neurograph, Motore ordini, Database, Prezzi, BrokerSync, Simulato, Telegram, Alpaca,
eToro). Il backend restituisce **eventi** già tradotti in percorsi di passaggi, ognuno con l'ora vera:

`{ id, ts, kind, who, path: [nodo, …], hops: [etichetta, …], outcome: "ok"|"error", latency_ms?, symbol? }`

Fonti e traduzione (regole dichiarate, nessuna deduzione fantasiosa):

1. **Chiamate MCP** (`mcp_audit_log`): ogni riga → `agente → Paperclip → MCP → <destinazione dello strumento>`
   e ritorno `→ MCP → Paperclip` quando ha senso. Destinazione per gruppo di strumenti, da una tabella nel
   codice: ordini/preview → Motore; dati di mercato/notizie/strumenti → Prezzi; `get_forecast` → Neurograph;
   backtest/ricerca/decisioni/attività/obiettivi/portafogli → Database; `send_telegram` → Telegram; altri →
   MCP (la cometa si ferma lì). `outcome != ok` → ultimo passaggio rosso. Etichetta = nome dello strumento.
2. **Ordini** (ordini e esecuzioni, solo portafogli in `total_portfolio_ids`): ordine inviato al broker →
   `Motore → BrokerSync → <broker del collegamento>`; esecuzione → `<broker> → BrokerSync → Motore`;
   ordine simulato eseguito → `Motore → Simulato`; rifiuto del broker → passaggio rosso.
3. **Regole automatiche** (`auto_trades` dei portafogli in campagna, oggi nessuno): entrata/uscita →
   `Prezzi → Regole automatiche → Motore`.
4. **Telegram** (`telegram_outbox` inviati): `Motore → Telegram` (o `MCP → Telegram` se nato da
   `send_telegram`, già coperto dal punto 1: non contarlo due volte).
5. **Prezzi** in arrivo (flussi in tempo reale): **non** come eventi (sarebbero centinaia al secondo); la
   corrente Prezzi→Motore ha una larghezza dal numero di aggiornamenti al minuto se il dato esiste già a costo
   zero, altrimenti resta sottile e senza comete.

### Rotte (sola lettura, amministratori)

- `GET /api/flow/events?after=<cursore>&limit=500` → `{ events: [...], cursor }`. Il cursore è opaco
  (per fonte: ultimo id letto). Prima chiamata senza cursore: ultimi 15 minuti (per riempire le larghezze).
- `GET /api/flow/summary?window=1|5|15` → per collegamento: chiamate nella finestra, strumenti più usati,
  errori; per nodo: passaggi al minuto; contatori di testa (chiamate/min, ordini di oggi, errori, tempo medio
  MCP). Calcolato in SQL con aggregazioni, non in Python riga per riga.

### Pagina (web)

- Voce di menu «Fiume dei flussi» vicino a «Log MCP», percorso `/fiume-flussi`.
- Disegno in Canvas 2D come la bozza (correnti, particelle, comete, onde, andamento 2 min); dati da TanStack
  Query con intervallo 2 s su `events` (con cursore) e 10 s su `summary`.
- Le comete si riproducono con l'ora vera, ritardate di ~2 s e distribuite nell'intervallo, così il traffico
  scorre fluido invece che a scatti; «velocità» cambia solo la riproduzione, «pausa» la ferma.
- «Segui un ordine»: aspetta il prossimo evento di tipo ordine (punto 2 o place_order) e lo evidenzia con la
  spiegazione passo per passo (testi della bozza).
- Scheda nodo: descrizione (testi della bozza), passaggi al minuto, ultimo passaggio vero (ora e strumento).
- Nessuna chiamata recente → riquadro discreto «nessuna chiamata negli ultimi N minuti».
- Pagina in pausa quando la scheda del browser non è visibile (niente letture inutili).

## Lasciato a chi costruisce

- Tabella strumento MCP → destinazione (completa, con test che fallisce se nasce uno strumento non mappato).
- Come leggere broker/stato dagli ordini esistenti (campi già presenti) senza query pesanti; indice nuovo solo
  se il piano di esecuzione lo richiede.
- Disposizione verticale dei nodi, colori e testi (dalla bozza), accessibilità di base (testo alternativo
  della tela con il riepilogo dei numeri).
- Test: backend (traduzione eventi per ogni fonte, filtro portafogli, cursore, limiti, permessi amministratore,
  catalogo MCP invariato), web (render, nessuna chiamata, errore di rete, pausa).

## Decisioni prese dopo la lettura del costruttore (scelte di Claude, 28/11)

1. **Tempi degli ordini**: nessuna tabella nuova. Gli eventi d'ordine nascono dai tempi già registrati:
   invio = `orders.broker_sent_at`, esecuzione = `fills.created_at`, rifiuto = `orders.updated_at` con
   `status = rejected`. Ogni evento ha un id stabile (`order:<id>:sent`, `fill:<id>`, `order:<id>:rejected`,
   `mcp:<id>`, `tg:<id>`, `auto:<id>:<fase>`); il cursore è un **tempo** (`after=<ts>`), la risposta
   comprende gli eventi con tempo > cursore − 10 s e la pagina scarta gli id già visti (niente doppioni, niente
   eventi persi per un aggiornamento tardivo). Indici nuovi solo se il piano di esecuzione lo chiede.
2. **Chiamate MCP**: si mostrano tutte le chiamate degli **agenti** (sono traffico vero, anche quando lavorano
   su un portafoglio di prova); si escludono le righe `admin:*` (azioni del portale, non degli agenti).
3. **Simbolo**: solo dalle chiavi `symbol` o `symbols` (primo elemento) di `params`; nient'altro dei parametri.
4. **Broker**: i nodi broker vengono dai collegamenti che esistono (`broker_links`), uno per tipo: «Alpaca
   (prova)», «Alpaca reale», «eToro (demo)», «eToro reale», «Trading 212 (demo)»; un tipo senza
   collegamenti non compare. La mappa è servita da `GET /api/flow/map`. Guardia: ogni tipo noto al codice ha
   un nome.
5. **Andata e ritorno**: un evento ha un solo `path` che comprende anche il ritorno quando c'è una risposta
   con dati (letture: `… → MCP → Paperclip`); ordini e messaggi solo andata.
6. **Tempo di una chiamata MCP**: la riga si scrive a fine chiamata; l'evento parte a
   `created_at − latency_ms`.
7. **Prezzi → Motore**: sottile e senza comete se non esiste già un contatore gratuito.
8. **Telegram**: `telegram_outbox.source` distingue `agent:<id>` (già coperto dalla chiamata
   `send_telegram`, non contato di nuovo) da `system`/`admin:*` (evento `Motore → Telegram`).
9. **Rotte**: GET solo amministratori, aggiunte a `ADMIN_ONLY_READS` e al resto dei cataloghi delle rotte come
   le altre letture (test_admin_users, test_user_activity).

## Sempre aggiornata (Matteo, 28/11)

«Ricordati di aggiornarla quando apporteremo modifiche in futuro, devono essere sempre reali i flussi.»
Ogni modifica futura a SMTrading che aggiunge, toglie o cambia un pezzo o un percorso (strumento MCP, broker o
collegamento, fonte prezzi, servizio esterno come Neurograph, nuovo canale di avvisi, nuovo modo di lavorare)
**aggiorna nello stesso lavoro** la mappa dei nodi, la tabella strumento → destinazione e i testi della pagina.
Guardie automatiche perché non si dimentichi:
- un test fallisce se esiste uno strumento MCP senza destinazione nella tabella;
- un test fallisce se esiste un tipo di collegamento broker (`broker_links.kind`) senza nodo;
- l'elenco dei nodi e dei percorsi vive in un solo modulo del backend, servito alla pagina da
  `GET /api/flow/map` (la pagina non tiene una copia propria dei nodi).

## Messa in linea

Build, backup, migrazione se c'è, ricreazione api/web con agenti fermi, controllo dal vero con il traffico
della giornata, Telegram con l'esito.
