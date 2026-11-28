# Dati per Neurograph e decisioni più ricche — progetto

Data: 28/11/2026 · Approvato da Matteo (scelte nella conversazione del 28/11) · Contratto confermato da Luca.

## Perché

Neurograph (Luca) ha bisogno dei dati che solo SMTrading raccoglie (decisioni degli agenti, ordini, esecuzioni)
per le sue ricerche, e di decisioni più ricche (fiducia dichiarata, risultato atteso, collegamento all'ordine)
per poter misurare quali decisioni portano a risultati buoni. In più get_forecast diventa sensibile agli eventi
in calendario (ramo di Luca) e la pagella misura il costo del calendario degli agenti.

## Cosa (deciso)

1. **get_forecast con eventi**: unire `origin/get-forecast-eventi` (commit 0c44ea3) a main, mantenendo la
   correzione efea71e (risposta di forma inattesa → `PROVIDER_UNAVAILABLE`). Aggiornare l'impronta del catalogo.
2. **Decisioni più ricche** (testo delle istruzioni approvato da Matteo):
   - `log_decision` accetta due campi facoltativi: `confidence` intero 0..100 e `expected`
     `{return_pct: numero, horizon_days: intero 1..365}`; salvati in colonne proprie (migrazione).
   - Gli strumenti degli ordini degli agenti (`place_order`, `place_bracket_order` e gli altri che creano
     ordini) accettano `decision_id` facoltativo: deve esistere ed essere dello stesso agente, altrimenti
     `INVALID_ORDER` con `details.field = "decision_id"`; salvato in `orders.decision_id` (migrazione).
   - Istruzioni comuni degli agenti (00), testo approvato: «In ogni `log_decision` indica `confidence` (da 0 a
     100, quanto sei sicuro) e, quando la decisione riguarda un risultato di mercato, `expected` con
     `return_pct` (rendimento atteso in %) e `horizon_days` (in quanti giorni). Prima di ogni ordine registra
     la decisione e passa il suo `decision_id` all'ordine.»
   - Messa in linea insieme al punto 1: l'elenco degli strumenti cambia una volta sola; ripartenza delle
     sessioni degli agenti automatica (`paperclip.sessions`).
3. **Invio notturno a Neurograph** (contratto confermato da Luca):
   - `POST {SMT_NEUROGRAPH_EXPORT_URL}/{decisioni|ordini|esecuzioni}/{AAAA-MM-GG}`; default dell'URL
     `http://192.168.4.4:18088/api/dati/smtrading`; chiave in `SMT_NEUROGRAPH_EXPORT_KEY` (`.env`, messa da
     Matteo; mai stampata né registrata); senza chiave l'invio è spento.
   - Intestazioni: `Authorization: Bearer <chiave>`, `Content-Type: application/x-ndjson`,
     `X-SMT-Export-Id: smtrading:{tipo}:{giorno}:{versione}`, `X-SMT-Rows: <n>`. Corpo JSON Lines, ≤ 64 MB.
   - Risposte di Neurograph: 201 primo invio; 200 «già presente» (versione ≤ salvata) o «sostituito»
     (versione più alta); 400 formato (non si riprova lo stesso file, avviso Telegram con il messaggio);
     401 chiave errata (stop e avviso Telegram); 503 ricezione non configurata e 5xx/timeout (si riprova la notte
     dopo).
   - Una volta per notte dopo la mezzanotte di Roma; recupero dei giorni mancanti fino a 30; un giorno
     confermato (201/200) non si rimanda; versione 1, più alta solo con una correzione esplicita (comando).
     Primo invio dallo storico del 23/11/2026. Comando per mandare **un solo giorno** (la prova chiesta da Luca).
   - Schema v1 con nomi in italiano (pagina «SMTrading per Neurograph»), più **negli ordini il campo
     `decisione`** (id della decisione che l'ha generato, vuoto se manca). Solo portafogli della campagna
     (`total_portfolio_ids`); nessun segreto, nessun testo dei messaggi Telegram; importi come stringhe
     decimali; tempi ISO UTC; il giorno è la giornata di Roma.
   - Stato degli invii salvato (tabella o `job_runs`) per idempotenza e recupero.
   - **Fiume dei flussi** (regola «sempre reale»): l'invio è un flusso nuovo `Database → Neurograph`; compare
     come evento `export:<tipo>:<giorno>:<versione>` con etichetta «esportazione <tipo>» ed esito.
4. **Costo del calendario nella pagella**: il formato §2 del documento di prontezza aggiunge «scarto di prezzo e
   ritardo per ora del giorno e per broker». La pagella la compila Claude: nessun codice.

## Rischio di portafoglio (futuro)

Quando Luca avrà lo strumento: le posizioni (simbolo, quantità, prezzo medio, valuta) a ogni chiamata, come le
chiusure di get_forecast. Non in questo lavoro.

## Vincoli

Campagna in corso: messa in linea ad agenti fermi, backup prima; MCP: cambi solo quelli elencati (catalogo
aggiornato una volta); nessuna lettura di Neurograph verso SMTrading; codice in inglese, istruzioni in italiano;
limite di lunghezza delle istruzioni (test_agent_docs_appendix).
