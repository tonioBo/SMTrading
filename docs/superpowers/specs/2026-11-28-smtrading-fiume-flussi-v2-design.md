# Fiume dei flussi v2 — progetto

Data: 28/11/2026 · Scelte di Matteo (moviola, storia di un ordine, risvegli e costi degli agenti, salute sui
nodi) + lente sui metodi (proposta di Claude in risposta a «andare un pelo più in dettaglio dei metodi… o
facciamo solo casino?»: dettaglio a richiesta, mai nella vista generale). Base: `2026-11-28-smtrading-fiume-flussi-design.md`.

## Regole che restano

Solo dati veri; sola lettura; nessuna modifica al percorso delle chiamate degli agenti; catalogo MCP invariato;
la vista generale resta pulita (stesse colonne e nodi); pagina solo amministratori; mappa sempre reale.

## 1. Moviola

- Scelta di un intervallo passato (preset: ultima ora, oggi, ieri, ultima seduta USA; oppure da–a) e
  riproduzione accelerata (1×, 10×, 60×, 300×) con barra di avanzamento, pausa, salto; stesso disegno del vivo,
  con un'etichetta chiara «moviola · ora mostrata».
- Dati: `GET /api/flow/events?from=&to=&limit=` (finestra chiusa, massimo 24 h, a pagine); le larghezze delle
  correnti calcolate sulla finestra riprodotta.

## 2. Storia di un ordine

- Clic su una cometa o una riga del diario (evento d'ordine o `place_order`) → pannello laterale con il
  viaggio completo in ordine di tempo: decisione dell'agente (sintesi, motivazione, `confidence`, `expected`,
  collegata tramite `orders.decision_id`; per gli ordini precedenti al collegamento: «decisione non collegata»),
  chiamata MCP, controlli e invio, figli (stop/obiettivo), esecuzioni con prezzo atteso ed eseguito, avviso
  Telegram (solo categoria e ora, mai il testo), esito.
- Dati: `GET /api/flow/order/{order_id}`; per una chiamata MCP senza ordine creato (rifiuto) la storia mostra la
  chiamata e il motivo del rifiuto.

## 3. Risvegli e costi degli agenti

- Ogni esecuzione di un agente in Paperclip = impulso `Paperclip → agente` (etichetta: motivo del risveglio:
  programmata, task, menzione) con esito (riuscita/errore/annullata).
- Su ogni nodo agente: spesa di oggi e del mese, tetto mensile; scheda con le ultime esecuzioni.
- Dati: lettura da Paperclip (stesso accesso già usato dal lettore Paperclip di SMTrading, mai il token alla
  pagina), con cache di almeno 30 s per non pesare su Paperclip.

## 4. Salute sui nodi

- Ogni nodo ha uno stato `ok | attenzione | errore | sconosciuto` mostrato come anello colorato con la
  spiegazione nella scheda: flusso prezzi (connesso/in collegamento/rifiutato, dallo stato dei flussi),
  collegamenti broker (stato del collegamento e interruzioni registrate), Neurograph (ultima risposta riuscita o
  fallita di get_forecast e dell'invio notturno), agenti (stato in Paperclip), Telegram (coda in uscita
  bloccata), database e servizi (salute del sistema già esistente).
- Dati: `GET /api/flow/health` (cache 15 s), ricavata dalle fonti di salute già esistenti.

## 5. Lente sui metodi (dettaglio a richiesta)

- Nella scheda di una corrente: tutti gli strumenti di quella corrente con chiamate, tempo medio ed errori
  nella finestra.
- Clic su «MCP SMTrading» (o un altro nodo) → riquadro «lente» sopra il fiume: il nodo si apre nelle famiglie
  di strumenti (ordini · dati di mercato · rischio e previsioni · ricerca · decisioni e attività · messaggi) come
  piccole correnti, ognuna con i suoi strumenti; comete vere anche nella lente; clic fuori per chiudere.
- Le famiglie vengono dal modulo della mappa (una sola fonte, con guardia: ogni strumento ha una famiglia).

## Fuori

Spinta in tempo reale (dopo il 23/12), modalità dimostrazione (esclusa da Matteo), azioni dalla pagina.
