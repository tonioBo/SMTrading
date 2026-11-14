# SMTrading MCP per Paperclip

SMTrading espone un server MCP (nome `SMTrading`) via HTTP streamable su `/mcp`.
Ogni agente ha un token personale; i permessi sono per portafoglio (`read` o `trade`).

## 1. Recuperare il token

Il token in chiaro viene mostrato **una sola volta**, quando il servizio `migrate` esegue il
seed e crea l'agente `paperclip-demo` (permesso `trade` sul portafoglio "Demo"):

```bash
docker compose logs migrate | grep "token agente"
```

Il token compare nei log **solo al primo avvio**, quando l'agente viene creato: i seed
successivi non lo ristampano (e i log del container possono essere stati ruotati o
cancellati). Formato: `smt_<prefix>_<secret>`.

Se il token e' andato perso, dalla web app: **Agenti → Rigenera token** sull'agente; il
nuovo token e' mostrato una sola volta e il vecchio smette di funzionare. In alternativa, da
CLI:

```bash
docker compose exec api python -m smtrading.cli agent rotate paperclip-demo
```

Altri comandi agenti della CLI (output JSON; i token non sono mai salvati in chiaro):

```bash
docker compose exec api python -m smtrading.cli agent list
docker compose exec api python -m smtrading.cli agent create NOME --portfolio Demo --permission trade
docker compose exec api python -m smtrading.cli agent deactivate NOME
```

Dopo `rotate` o `deactivate` il vecchio token viene rifiutato subito dal processo che ha
eseguito il comando; gli altri processi lo rifiutano al piu' entro 60 secondi (cache dei
token). Un token sbagliato ripresentato viene rifiutato senza ricalcolare l'hash per 60
secondi.

## 2. Collegare Paperclip alla rete

```bash
scripts/connect-paperclip.sh            # container "paperclip" (default)
scripts/connect-paperclip.sh altro-nome
```

Lo script esegue `docker network connect smtrading-net <container>` ed e' idempotente:
se il container e' gia' sulla rete non fa nulla.

## 3. Configurare il server MCP in Paperclip

In Paperclip: **Connectors → Connect your own MCP server**.

- URL: `http://api:8000/mcp` (oppure `http://smtrading-api-1:8000/mcp`)
- **Advanced → authentication header**: `Authorization: Bearer <token>`
- **Permissions**: *Just agents I pick*, scegliendo gli agenti che devono usarlo
- Azioni di scrittura (ordini, watchlist, alert, backtest, ...): **Allowed** solo per
  l'agente che deve fare trading; per gli altri lasciale non consentite
- Dopo ogni aggiornamento di SMTrading che aggiunge o cambia metodi: **Refresh actions**

Configurazione equivalente per client MCP generici:

```json
{
  "mcpServers": {
    "smtrading": {
      "type": "http",
      "url": "http://api:8000/mcp",
      "headers": { "Authorization": "Bearer smt_xxxxxxxx_..." }
    }
  }
}
```

Verifica rapida dal host:

```bash
docker compose exec api python -m smtrading.mcp.smoke --url http://localhost:8000/mcp --token <token>
```

Senza token (o con un token non valido) il server risponde HTTP 401 con
`{"code":"FORBIDDEN","message":"invalid token"}`.

La porta 8000 e' pubblicata solo su `127.0.0.1` dell'host (accesso locale); Paperclip
raggiunge l'API attraverso la rete docker `smtrading-net`.

Se per la verifica si e' creato un agente dedicato (es. `smoke-agent`), disattivarlo quando
non serve piu':

```bash
docker compose exec api python -m smtrading.cli agent deactivate smoke-agent
```

## 4. Metodi

Portafogli collegati a un broker paper/demo (pagina *Broker*): per l'agente gli ordini
funzionano allo stesso modo, ma l'esecuzione e' asincrona. `place_order` risponde con
l'ordine in stato aperto; il fill (o il rifiuto del broker) arriva dopo, quindi va
controllato con `get_order`/`list_orders`. Un annullamento resta in attesa finche' il broker
non lo conferma. Su questi portafogli nulla si esegue localmente: gli ordini non
instradabili al broker sono rifiutati (subito o, se il broker non riconosce lo strumento,
poco dopo con una notifica `order.rejected`), il trailing stop diventa al trigger un market
reduce-only inviato al broker. Un portafoglio archiviato e' invisibile all'agente
(`NOT_FOUND`).

Passa i numeri come stringhe per mantenere la precisione esatta (es. `"qty": "0.1"`).
Gli agenti non possono creare portafogli, depositare, prelevare o modificare i limiti.

- `list_portfolios`: i portafogli utilizzabili dall'agente, con il permesso (`read`/`trade`).
  I portafogli archiviati dall'amministratore non compaiono e, cercati per id, danno
  `NOT_FOUND` (l'assegnazione resta e torna valida quando vengono ripristinati).
  Il campo `mode` (anche in `get_portfolio`) indica la modalita': `simulazione` (nessun broker
  collegato, denaro virtuale), `paper` (collegato a un conto demo) o `reale` (riservato, non ancora usato).
- `get_portfolio`: valutazione mark-to-market (EUR): equity, esposizione, margini, potere d'acquisto, posizioni, P&L del giorno.
- `get_positions`: posizioni aperte con prezzo medio, mark e P&L non realizzato.
- `get_transactions`: movimenti del ledger del portafoglio, dal piu' recente, con filtri e paginazione (`before_seq`).
- `get_performance`: serie dell'equity e statistiche (rendimento, max drawdown, sharpe) su un periodo.
- `get_risk_limits`: stato di rischio: soglie di margine, profilo effettivo (con `description`, il «Quando usarlo» del profilo), perdita giornaliera, kill switch.
- `preview_order`: esegue i controlli pre-trade senza inserire l'ordine (stima di prezzo, costi, potere d'acquisto).
- `place_order`: inserisce un ordine (market, limit, stop, stop_limit, trailing_stop); `client_order_id` rende l'invio idempotente.
- `place_bracket_order`: ingresso piu' take profit e stop loss in un gruppo OCO.
- `place_oco_order`: due ordini a riposo collegati: quando uno si esegue l'altro viene annullato.
- `modify_order`: modifica quantita' o prezzi di un ordine aperto, con nuovi controlli di rischio.
- `cancel_order`: annulla un ordine aperto.
- `cancel_all_orders`: annulla tutti gli ordini aperti del portafoglio (opzionalmente di un solo simbolo).
- `get_order`: un ordine con i suoi fill.
- `list_orders`: ordini del portafoglio, dal piu' recente, filtrabili per stato.
- `close_position`: chiude una posizione (o parte di essa) con un ordine market reduce-only.
- `close_all_positions`: chiude tutte le posizioni aperte con ordini market reduce-only.
- `search_instruments`: cerca strumenti per simbolo o nome (al massimo 50 risultati), filtrabili per `asset_class` e `market`: prima il catalogo (`in_catalogo: true`), poi, con `include_provider` (default `true`), i risultati dei provider non ancora in catalogo che si possono aggiungere (`in_catalogo: false`, `aggiungibile: true`). La ricerca non crea nulla: per aggiungerne uno basta chiamare `get_instrument(symbol)` (o quotarlo, o fare trading).
- `get_instrument`: anagrafica di uno strumento e stato del suo mercato; un simbolo non in catalogo viene aggiunto automaticamente (vedi sotto).
- `get_quote`: ultime quotazioni (bid, ask, last) fino a 50 simboli.
- `get_bars`: barre OHLCV per timeframe e intervallo.
- `get_market_status`: apertura/chiusura e prossimi orari di uno o tutti i mercati.
- `get_indicators`: indicatori tecnici (sma, ema, rsi, atr, bollinger, macd, vwap).
- `get_fx_rate`: cambio tra due valute.
- `get_news`: ultime notizie per simboli (default: quelli in portafoglio).
- `get_fundamentals`: dati fondamentali: settore, capitalizzazione, P/E, EPS, dividend yield.
- `get_earnings_calendar`: eventi di utili in un intervallo di date.
- `get_dividends`: eventi di dividendo di un simbolo.
- `screen`: selezione di strumenti per filtri.
- `get_triggered_alerts`: notifiche dei portafogli dell'agente (o indirizzate a lui): `{alerts, next_after_id}`; senza cursore dalla piu' recente, con `since`/`after_id` dalla piu' vecchia; `limit` 1-500.
- `list_watchlists`: le watchlist dell'agente con i loro simboli.
- `create_watchlist`: crea una watchlist vuota (nome unico per agente, al massimo 20).
- `add_to_watchlist`: aggiunge simboli (del catalogo, o aggiunti automaticamente) a una watchlist (al massimo 200 per watchlist); le loro quotazioni vengono seguite in streaming.
- `remove_from_watchlist`: toglie simboli da una watchlist.
- `delete_watchlist`: elimina una watchlist.
- `create_alert`: crea un alert di prezzo (al massimo 100 attivi per agente): `price_above`/`price_below` (valore = prezzo) o `change_pct_above`/`change_pct_below` (valore = variazione percentuale dal prezzo alla creazione, es. `-5`). Il prezzo e' `last`, altrimenti il mid; scatta una sola volta e arriva in `get_triggered_alerts` come notifica `price_alert` (`payload`: `alert_id`, `symbol`, `condition`, `value`, `price`).
- Modifiche dell'amministratore: se l'admin rimuove un simbolo da una watchlist, elimina una watchlist o annulla un alert, l'agente riceve in `get_triggered_alerts` una notifica `admin.watch_changed` (`payload`: `action` = `remove_symbol` | `delete_watchlist` | `cancel_alert`, `watchlist`, `symbol` o `alert_id`).
- `list_alerts`: gli alert dell'agente, filtrabili per stato (`active`/`triggered`).
- `delete_alert`: elimina un alert dell'agente.

### Strumenti: catalogo e aggiunta automatica

Il catalogo contiene circa 400 strumenti verificati: azioni ed ETF USA (Nasdaq-100, S&P 100, principali ETF), Europa (FTSE MIB `.MI`, DAX `.DE`, CAC 40 `.PA`, ETF UCITS su Xetra in EUR), le principali crypto in USD (`BTC-USD`, ...) e le coppie forex maggiori e incrociate (`EURUSD`, ...). Mercati: `NASDAQ`, `NYSE`, `MTA`, `XETRA`, `EURONEXT_PARIS`, `CRYPTO`, `FX`.

Quando un agente usa un simbolo che non e' in catalogo (`get_instrument`, `get_quote`, `get_bars`, `get_indicators`, `preview_order`, `place_order` e varianti, `create_alert`, `add_to_watchlist`, `run_backtest`), la piattaforma prova ad aggiungerlo:

- accettati: azioni ed ETF su NASDAQ/NYSE in USD, `.MI`/`.DE`/`.PA` in EUR, crypto `XXX-USD`, forex `XXXYYY`, solo con una quotazione attuale a prezzo positivo;
- rifiutati con `INVALID_ORDER` "Strumento non supportato: <motivo>": OTC, LSE/GBX e altre borse, fondi, crypto non in USD, prezzo nullo; un simbolo che nessun provider conosce resta `SYMBOL_NOT_FOUND`; un guasto dei provider e' `PROVIDER_UNAVAILABLE` (si puo' riprovare);
- al massimo 25 aggiunte per agente al giorno (oltre: `RISK_LIMIT_EXCEEDED`); l'admin puo' disattivare l'aggiunta automatica dalla pagina Mercati (allora un simbolo sconosciuto e' `SYMBOL_NOT_FOUND`);
- uno strumento disattivato dall'admin non viene mai riattivato;
- lo strumento aggiunto resta in catalogo anche se l'operazione che l'ha richiesto fallisce, con origine "auto: agente <nome>", una riga di audit e una notifica `instrument.added`.

### Backtest

Simulazione pura di una strategia solo long su barre storiche: non tocca portafogli, ordini ne' broker.

- `run_backtest`: mette in coda un backtest e risponde `{backtest_id, status: "queued"}`. Parametri: `strategy` (`sma_cross` con `fast=20`, `slow=50`; `rsi_reversion` con `period=14`, `buy_below=30`, `sell_above=70`; `breakout` con `lookback=20`, `exit_lookback=10`; `buy_and_hold`), `symbols`, `from_date`, `to_date`, `timeframe` (default `1d`), `initial_cash` (default `10000`, diviso in parti uguali tra i simboli), `params`, `portfolio_id` (opzionale, serve `read`: ne usa le commissioni e lo slippage, altrimenti la regola globale), `slippage_bps` (opzionale; precedenza: parametro, portafoglio, 5 bps). La commissione e' risolta per ogni simbolo (classe di attivita'); la parte fissa e i limiti min/max sono convertiti dalla valuta della regola a quella dello strumento al cambio dell'inizio del backtest, e `fx_pct` si somma alla percentuale per le operazioni in valuta diversa; senza cambio disponibile resta la regola cosi' com'e' con il warning `fee_currency_unconverted:EUR->USD`. Con un periodo inferiore a un anno il CAGR e' annualizzato da un periodo breve (warning `cagr_annualised_from_short_span`). I segnali si eseguono all'apertura della barra successiva con slippage. Al massimo 2 backtest in coda o in esecuzione per agente (`RATE_LIMITED`), al massimo 5 anni di barre giornaliere o 90 giorni di barre intraday (`RISK_LIMIT_EXCEEDED`).
- `get_backtest`: stato e risultato di un backtest dell'agente (di un altro agente: `FORBIDDEN`). Il `result` contiene `metrics` (`total_return_pct`, `cagr_pct`, `max_drawdown_pct`, `sharpe`, `win_rate_pct`, `trades`, `exposure_pct`), `equity` (al massimo 1000 punti) e `trades`; un backtest fallito ha `error`.
- `list_backtests`: i backtest dell'agente, dal piu' recente, senza curva e operazioni; filtro `status`, `limit` 1-100.

### Blocco aperture (CEO)

- `set_trading_halt`: attiva (`active: true`) o revoca (`active: false`) il blocco delle nuove aperture. Serve il permesso `halt` (di solito solo il CEO; senza: `FORBIDDEN` con `details.needed = "halt"`). `scope` `global` (tutti i portafogli, default) o `portfolio` (con `portfolio_id`, serve `read` su quel portafoglio); `reason` 1-500 caratteri, obbligatoria per attivare (i caratteri di controllo sono rimossi). Risponde `{active, changed, halt}`: la stessa motivazione una seconda volta non cambia nulla (`changed: false`), una motivazione diversa sostituisce il blocco; revocare senza blocco attivo da' `{active: false, changed: false, halt: null}`. SMTrading invia da se' il messaggio Telegram critico (`halt.set` / `halt.cleared`, `already_sent` nel feed): non ripeterlo.
- `get_trading_halt`: aperto a tutti gli agenti. I blocchi attivi che coprono i portafogli dell'agente (o solo `portfolio_id`, serve `read`): `{halted, halts: [{id, scope, portfolio_id, portfolio, reason, set_by, since}]}`, dal piu' recente; `set_by` e' il nome dell'agente o `admin`.

Mentre un blocco copre un portafoglio, ogni ordine che aumenterebbe l'esposizione (nuove posizioni, incrementi, aperture short, ingresso di bracket e OCO) e' rifiutato con `TRADING_HALTED` e messaggio `Aperture sospese dal CEO: <motivo>`; il rifiuto resta registrato come ordine `rejected`. Restano permessi gli ordini che riducono o chiudono (`close_position`, vendite fino alla quantita' posseduta), gli stop loss / take profit delle posizioni esistenti, gli annullamenti, le liquidazioni e la sincronizzazione col broker. Un ordine di apertura gia' in attesa (limit, stop) e' rifiutato quando scatterebbe. Il controllo e' quello del kill switch (che ha la precedenza) e vale per portafogli simulati, paper e reali. Il permesso si concede dalla scheda dell'agente o con `python -m smtrading.cli agent halt-permission NOME on`.

### Decisioni, feed delle attivita' e Telegram

- `log_decision`: registra una decisione nello storico permanente (visibile nella web app): `kind` (`proposal`, `approval`, `veto`, `allocation`, `strategy`, `risk_rule`, `other`), `summary` (1-500 caratteri), `portfolio_id` facoltativo (serve `read`), `symbols` (al massimo 50, max 32 caratteri l'uno, portati in maiuscolo e senza duplicati), `details` (oggetto, al massimo 8 KB). Risponde `{decision_id, created_at}`; non invia messaggi Telegram.
- `get_decisions`: le decisioni visibili all'agente, dalla piu' recente, filtrabili per `portfolio_id` (serve `read`, altrimenti `FORBIDDEN`), `agent` (nome o id; un agente sconosciuto da' `[]`), `kind`, `since`; `limit` 1-200 (default 50).
- `get_activity_feed`: il feed delle attivita' (ordini, fill, eventi di rischio, alert, decisioni) dei portafogli dell'agente e globali: `{items, next_after_id}`; `limit` 1-200. Vedi sotto il contratto del cursore.
- `send_telegram`: invia un messaggio al gruppo Telegram (serve il permesso `notify`, altrimenti `FORBIDDEN`): `text` inviato cosi' com'e', senza formattazione; `level` `info` (nessun prefisso), `warning` (🟠) o `critical` (🔴). Limiti e errori nella sezione «Notifiche Telegram».

#### Decisioni

Ogni agente registra con `log_decision` ogni decisione presa in Paperclip (proposte,
approvazioni, veti, allocazioni, strategie, regole di rischio, altro) e poi coinvolge il
Notifier con un task o una menzione: il Notifier non fa polling. Le decisioni restano nello
storico (web app, *Notifiche*) e compaiono nel feed e nei riepiloghi. `portfolio_id`
facoltativo: senza, la decisione e' globale e visibile a tutti gli agenti; con, serve il
permesso `read` su quel portafoglio. Violazioni dei limiti (tipo, lunghezza, simboli,
dimensione di `details`) danno `INVALID_ORDER` con `details.field`.

#### Feed delle attivita'

Tipi: `order.accepted`, `order.filled`, `order.partially_filled`, `order.rejected`,
`order.canceled`, `order.expired`, `kill_switch`, `portfolio.reset`, `portfolio.cash`, gli
avvisi di rischio e del broker (`liquidation`, `daily_loss`, `margin_warning`,
`broker.order_unknown`, `broker.liquidation_waiting`, `broker.fill_price_missing`,
`broker.reconciliation_diff`, `order.replace_failed`), `price_alert`, `instrument.added`,
`split_orders_canceled`, `decision.<kind>` e ogni altro tipo di notifica (livello `info`).
Livelli: `critical`, `warning`, `executed`, `info`. Ogni elemento:
`{id, ts, type, level, agent, agent_id, portfolio, portfolio_id, summary, data, already_sent}`.

Contratto del cursore (come «Polling degli alert»):

1. Prima chiamata senza `since`/`after_id`: gli ultimi `limit` elementi, dal piu' recente, e
   `next_after_id` = id del piu' recente tra quelli assestati (`null` se non ce ne sono).
2. Chiamate successive con `after_id = next_after_id`: solo gli elementi piu' nuovi, in
   ordine crescente di id; `next_after_id` e' l'ultimo id restituito oppure lo stesso cursore.
   Con `since` si parte dal piu' vecchio dopo quella data. Un `after_id` sconosciuto da'
   `INVALID_ORDER` (`details.field = after_id`).
3. **Finestra di assestamento di 3 s**: un elemento e' visibile (e il cursore lo supera)
   solo quando ha almeno 3 secondi, cosi' una transazione ancora aperta non viene saltata.
   Un elemento appena creato compare quindi alla chiamata successiva.
4. Se una pagina contiene `limit` elementi richiamare subito. Gli elementi possono ripetersi
   (per esempio alla ripartenza da `since`): **deduplicare per `id`**.
5. `already_sent: true`: SMTrading lo invia o lo inviera' da se' su Telegram (gia'
   inviato, in coda, nel riepilogo della notte o non ancora pianificato, per esempio con il
   worker fermo; resta `true` anche se poi e' scartato perche' in pausa da piu' di 24 ore). **Non ripeterlo**: quando il worker riparte SMTrading lo invia comunque.
   `already_sent: false` solo per gli eventi che SMTrading non inviera' mai (Telegram
   disattivato in quel momento, evento vecchio scartato, errore) e per quelli che non
   notifica da se'.

#### Notifiche Telegram

SMTrading invia da se', senza Paperclip: 🔴 critico (kill switch attivato, liquidazione,
perdita giornaliera, ordine sconosciuto al broker), 🟠 avviso (ordine rifiutato, kill
switch disattivato, margine, problemi del broker, differenze di riconciliazione), 🟢 ordine
eseguito. Dalle 23:00 alle 07:00 solo i critici arrivano (senza suono); il resto e' nel
riepilogo delle 07:00. I messaggi del Notifier (`send_telegram`) di notte partono subito,
senza suono: non finiscono nel riepilogo delle 07:00. Riepiloghi automatici alle 18:00 e il venerdi' alle 18:30 con la
classifica dei portafogli. Configurazione e interruzioni: README, «Notifiche Telegram».

Limiti di `send_telegram`:

- serve il permesso `notify` dell'agente (si concede dalla scheda dell'agente o con
  `python -m smtrading.cli agent notify NOME on`); senza: `FORBIDDEN`;
- al massimo 20 messaggi in 60 secondi per agente: oltre `RATE_LIMITED` con
  `details.retry_after_s`;
- la lunghezza e' contata in unita' UTF-16 (un'emoji vale 2) e comprende il prefisso del
  livello: massimo 4096; il testo (senza spazi iniziali e finali, caratteri di controllo
  tolti) vuoto o troppo lungo da' `INVALID_ORDER` (`details.field`);
- Telegram non configurato o disattivato: `PROVIDER_UNAVAILABLE`;
- notifiche in pausa: il messaggio e' accettato e resta in coda (`status: "paused"`),
  partira' alla ripresa; altrimenti `status: "pending"` e risponde `{outbox_id, status}`.

#### Prompt suggerito per il Notifier

Da incollare nelle istruzioni dell'agente Notifier in Paperclip (permesso `notify`,
accesso `read` ai portafogli):

```text
Sei il Notifier di SMTrading. Lavori solo su richiesta, senza polling: ti attivi quando un
altro agente ti assegna un task o ti menziona dopo aver registrato una decisione con
log_decision.

Per ogni richiesta:
1. Leggi get_activity_feed a partire dal tuo ultimo cursore (after_id) e get_decisions per
   le decisioni recenti; deduplica per id e aggiorna il cursore.
2. Non ripetere gli elementi con already_sent: true: SMTrading li invia (o li inviera')
   da solo.
3. Scrivi messaggi brevi in italiano: al massimo 10 righe, testo semplice senza markup,
   numeri con la virgola decimale. Raggruppa piu' decisioni in un solo messaggio.
4. Invia con send_telegram. Usa level=critical solo per rischi reali, warning per avvisi
   che richiedono attenzione, info per il resto.
5. I riepiloghi delle 18:00 e del venerdi' alle 18:30 li invia SMTrading da solo: se vuoi
   puoi aggiungere dopo un breve commento alle decisioni del giorno, mai una copia del
   riepilogo.
6. Rispetta il limite di 20 messaggi al minuto (con RATE_LIMITED attendi retry_after_s) e
   non inviare mai token, ID interni o dati personali.
```

### Polling degli alert

1. Prima chiamata senza `since`/`after_id`: le notifiche piu' recenti (dalla piu' recente) e
   `next_after_id` = id della piu' recente.
2. Chiamate successive con `after_id = next_after_id`: solo le notifiche create dopo, in
   ordine crescente (`created_at`, `id`), e un nuovo `next_after_id` (l'ultimo id ricevuto,
   o lo stesso cursore se non c'e' nulla di nuovo).
3. Se una pagina contiene `limit` notifiche, richiamare subito: potrebbero essercene altre.

### Indicatori

`get_indicators` restituisce `specs: [{key, name, params}]` e i valori sotto `key`: il nome
dell'indicatore, oppure `nome_<parametri>` (es. `sma_20`, `sma_50`) quando lo stesso
indicatore e' richiesto piu' volte. I periodi sono interi tra 1 e 1000.

### Formato dei numeri

I Decimal sono stringhe in notazione decimale semplice, senza esponente e senza zeri finali
(`"0"`, `"5"`, `"0.0000001"`, `"99950.5"`). `get_performance` restituisce al massimo 500
punti: per i periodi da `1m` in su solo i punti di apertura giornaliera (`day_open`); le
statistiche sono calcolate su tutti gli snapshot del periodo.

## 5. Errori

Un errore e' restituito con `isError: true` e un testo JSON
`{"code": "...", "message": "...", "details": {...}}`. Codici:

- `INSUFFICIENT_BUYING_POWER`: potere d'acquisto insufficiente.
- `MARKET_CLOSED_QUEUED`: mercato chiuso, ordine messo in coda.
- `SYMBOL_NOT_ALLOWED`: simbolo non consentito dal profilo di rischio.
- `SYMBOL_NOT_FOUND`: simbolo sconosciuto.
- `KILL_SWITCH_ACTIVE`: kill switch globale attivo.
- `TRADING_HALTED`: blocco aperture attivo (dal CEO o dall'admin): sono permessi solo ordini che riducono o chiudono; il motivo e' nel messaggio, `get_trading_halt` dice chi e da quando.
- `REDUCE_ONLY`: il portafoglio accetta solo ordini che riducono le posizioni.
- `PORTFOLIO_LOCKED`: portafoglio bloccato.
- `PORTFOLIO_ARCHIVED`: portafoglio archiviato (sola lettura); un agente vede invece `NOT_FOUND`.
- `STALE_QUOTE`: quotazione troppo vecchia.
- `RISK_LIMIT_EXCEEDED`: limite di rischio superato.
- `SHORT_NOT_ALLOWED`: vendita allo scoperto non consentita.
- `INVALID_ORDER`: argomenti non validi, compresi argomenti mancanti o del tipo sbagliato (il campo e' in `details.field`).
- `NOT_FOUND`: risorsa inesistente.
- `FORBIDDEN`: token non valido, portafoglio non assegnato o permesso insufficiente (`notify`, `halt`: `details.needed`).
- `RATE_LIMITED`: troppe richieste.
- `PROVIDER_UNAVAILABLE`: dati di mercato non disponibili.
- `INTERNAL`: errore interno inatteso. Non fa parte dei codici della spec: il messaggio e'
  sempre generico (`"internal error"`), il dettaglio e' solo nei log del server e la chiamata
  e' registrata nell'audit con esito `INTERNAL`. Ripetere piu' tardi o segnalarlo.

Altri codici del ledger (`INSUFFICIENT_FUNDS`, `INVALID_AMOUNT`, ...) riguardano operazioni
non esposte agli agenti.
