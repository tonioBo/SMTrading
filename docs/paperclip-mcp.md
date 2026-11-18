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

### Nuovi strumenti: sessioni degli agenti ripartite da sole

Un agente Paperclip tiene la sua sessione (e l'elenco degli strumenti ricevuto) tra un
giro e l'altro. Quando una nuova versione di SMTrading cambia gli strumenti MCP (nomi,
descrizioni o parametri), il worker se ne accorge (impronta sha256 del catalogo) e, se la
lettura Paperclip e' configurata e attiva, azzera la sessione di ogni agente dell'azienda
che non sta lavorando (`POST /api/agents/{id}/runtime-state/reset-session`, l'unica
scrittura verso Paperclip). Gli agenti in esecuzione vengono ripresi ai controlli
successivi (ogni 5 minuti); a lavoro finito arriva un solo messaggio Telegram «Strumenti
degli agenti aggiornati: sessioni ripartite per N agenti». Alla prima installazione
l'impronta viene solo memorizzata. Errori e agenti in attesa compaiono nello stato
Paperclip del portale; un azzeramento che fallisce viene ritentato al massimo 3 volte.

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
  collegato, denaro virtuale), `paper` (collegato a un conto demo) o `reale`.
  **Per default (impostazione `mcp_hide_mode`, attiva) il campo `mode` e' omesso per i portafogli
  non reali**: gli agenti non sanno che un portafoglio e' simulato o paper, per loro e' tutto reale.
  L'impostazione si cambia dal portale (Impostazioni, «Nascondi agli agenti la modalita' dei
  portafogli (simulazione/paper)») o con `PUT /api/settings/app` (`{"mcp_hide_mode": false}`);
  portale e Telegram mostrano sempre la modalita'. Le descrizioni dei tool non citano la simulazione
  dei portafogli (i backtest sono «test su dati storici»).
- `get_portfolio`: valutazione mark-to-market (EUR): equity, esposizione, margini, potere d'acquisto, posizioni, P&L del giorno.
- `get_positions`: posizioni aperte con prezzo medio, mark e P&L non realizzato; le posizioni
  USA hanno anche `extended` (come in `get_quote`).
- `get_transactions`: movimenti del ledger del portafoglio, dal piu' recente, con filtri e paginazione (`before_seq`).
- `get_performance`: serie dell'equity e statistiche (rendimento, max drawdown, sharpe) su un periodo.
- `get_risk_limits`: stato di rischio: soglie di margine, profilo effettivo (con `description`, il «Quando usarlo» del profilo), perdita giornaliera, kill switch.
- `preview_order`: esegue i controlli pre-trade senza inserire l'ordine (stima di prezzo, costi, potere d'acquisto).
- `place_order`: inserisce un ordine (market, limit, stop, stop_limit, trailing_stop); `client_order_id` rende l'invio idempotente.
  Su alcuni portafogli collegati a un broker sono ammessi solo gli ordini a mercato: gli
  altri tipi sono rifiutati con `INVALID_ORDER` e il messaggio «tipo d'ordine non
  disponibile su questo broker: solo ordini a mercato» (il trailing stop resta ammesso: e'
  seguito da SMTrading e al trigger diventa un ordine a mercato).
- `place_bracket_order`: ingresso piu' take profit e stop loss in un gruppo OCO.
- Campo facoltativo `valid_until` di `place_order` e `place_bracket_order`: data e ora ISO con fuso
  (es. `2026-11-18T21:55:00+01:00`) o `"close"` (chiusura regolare di oggi del mercato dello
  strumento; non per crypto e FX). Solo per l'ingresso: se a quell'ora non e' eseguito,
  SMTrading lo annulla (anche presso il broker, con l'annullamento normale) e l'ordine
  risulta `expired` (`order.expired`), con le uscite bracket annullate; la parte gia'
  eseguita resta, con stop e target sulla qty eseguita. Il valore e' nel campo
  `valid_until` degli ordini. Un valore non valido (passato, senza fuso, mercato gia' chiuso
  oggi) e' `INVALID_ORDER` con `field: valid_until`.
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
- `get_quote`: ultime quotazioni (bid, ask, last) fino a 50 simboli. Le azioni USA hanno
  anche `extended`: con la seduta regolare chiusa, l'ultimo prezzo pre-market o after-hours
  `{session: "pre"|"post", price, change_pct_vs_close, ts}` (variazione in % sull'ultima
  chiusura regolare), altrimenti `null`. Orari estesi: solo informativo, ordini e stop si
  valutano solo in seduta regolare (nessun trading negli orari estesi; mark, P&L, segnali e
  indicatori restano sulla seduta regolare). Fonte: Yahoo, cache 60 s.
- `get_bars`: barre OHLCV per timeframe e intervallo.
- `get_market_status`: apertura/chiusura e prossimi orari di uno o tutti i mercati
  (`NASDAQ`, `NYSE`, `MTA`, `XETRA`, `EURONEXT_PARIS`, `CRYPTO`, `FX` e `LSE`). Le chiusure
  straordinarie decise dall'admin (pagina *Mercati*: giorno chiuso o chiusura anticipata)
  hanno la precedenza sul calendario e sono gia' comprese negli orari. `LSE` (Londra) c'e'
  **solo per gli orari**: i titoli londinesi non sono negoziabili (non entrano in catalogo).
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
- Modifiche dell'amministratore: se l'admin rimuove un simbolo da una watchlist, elimina una watchlist o annulla un alert, l'agente riceve in `get_triggered_alerts` una notifica `watch_changed` (`payload`: `action` = `remove_symbol` | `delete_watchlist` | `cancel_alert`, `watchlist`, `symbol` o `alert_id`).
- `list_alerts`: gli alert dell'agente, filtrabili per stato (`active`/`triggered`).
- `delete_alert`: elimina un alert dell'agente.

### 🔴 Portafogli reali: un portafoglio `reale` usa denaro vero

*Questa sezione e' per gli operatori umani: gli agenti hanno le loro istruzioni (pagina
«Istruzioni agenti», `docs/paperclip-agents`).* Al momento **non esiste nessun
collegamento live**: tutto quanto segue e' pronto ma non attivato (vedi «Denaro reale» nel
README).

- Un portafoglio collegato a un conto reale (Alpaca live, Trading 212 Invest, eToro) con il denaro
  reale attivo invia gli ordini degli agenti al **conto vero**, senza alcuna conferma per
  ordine, entro i limiti del portafoglio. Ogni ordine reale produce una notifica Telegram.
  L'esecuzione e' asincrona come per i portafogli paper (`place_order` risponde con
  l'ordine aperto; fill o rifiuto arrivano dopo, da controllare con `get_order`).
- Stato del collegamento, `live_state`: `configured` (sospeso, nessun invio), `dry_run`
  (prova a secco: gli ordini passano da rischio e invio ma si fermano prima del broker),
  `armed` (denaro reale). In `list_portfolios`/`get_portfolio`:
  - con `mcp_hide_mode` attivo (default) l'agente vede `mode: "reale"` e
    `live_state: "armed"` **solo** per un collegamento attivo; per `dry_run` e
    `configured` i due campi sono omessi e il portafoglio appare come qualsiasi altro;
  - con `mcp_hide_mode` spento vede sempre `mode` (`reale` solo se `armed`, altrimenti
    `paper`) e `live_state`.
- **Prova a secco e modalita' nascosta:** con `mcp_hide_mode` attivo gli agenti non vedono
  mai `LIVE_DRY_RUN` ne' gli stati `dry_run`/`configured`: un ordine fermato dalla prova a
  secco appare all'agente esattamente come un ordine rifiutato dal broker senza motivo:
  errore/`reject_code` `PROVIDER_UNAVAILABLE` con messaggio `"rejected by the broker"` e
  attivita' `live.order.rejected` (livello `warning`, senza il payload). Il portale
  (ordine rifiutato con il payload salvato) e Telegram mostrano invece la prova a secco.
  Con `mcp_hide_mode` spento l'agente riceve `LIVE_DRY_RUN`.
- **Nessuna traccia di simulazione per gli agenti:** con `mcp_hide_mode` attivo le
  esecuzioni (`fills` di `get_order`/`list_orders`, `data` delle attivita'
  `order.filled`/`order.partially_filled`) non hanno il campo `source` (simulatore o
  broker), e host/tipi dei broker paper/demo nei testi appaiono come quelli reali: un
  portafoglio simulato e uno reale si leggono allo stesso modo. Portale e Telegram restano
  invariati.
- **Nessun metodo MCP attiva, collega, scollega o disattiva conti reali**, ne' toglie un
  reduce-only automatico del denaro reale o chiude tutte le posizioni reali: lo fa solo
  l'admin dal portale (attivazione, collegamento, ripresa e chiusura con TOTP) o, per la
  sola disattivazione, con `python -m smtrading.cli live disarm`. I token degli agenti non hanno alcun
  permesso in piu' per i portafogli reali.
- **Il kill switch blocca anche le chiusure**: su un portafoglio reale ogni invio e'
  rifiutato con `KILL_SWITCH_ACTIVE`, compresi `close_position`, `close_all_positions` e gli
  ordini reduce-only; passano solo le cancellazioni (`cancel_order`, `cancel_all_orders`) e
  gli ordini di «Chiudi tutte le posizioni reali» dell'admin.
- Controlli in piu' prima dell'invio: niente vendite allo scoperto (`SHORT_NOT_ALLOWED`);
  tetto di liquidita' (`LIVE_CASH_LIMIT`, vedi sotto); fusibile di frequenza (al massimo
  `max_live_orders_per_hour` ordini inviati per portafoglio in un'ora, default 30): superato
  → `RISK_LIMIT_EXCEEDED` e il portafoglio passa in reduce-only.
- Piu' portafogli possono condividere un conto reale, ognuno con la sua quota di contante:
  per l'agente non cambia nulla (vede solo i suoi portafogli; un acquisto deve stare nel
  contante del suo portafoglio, altrimenti `LIVE_CASH_LIMIT`). Disattivazione, prova a
  secco, kill switch e fusibile valgono per tutto il collegamento.
- Un'anomalia della sorveglianza (conto cambiato, differenze di riconciliazione, esito
  sconosciuto di un ordine, fusibile) mette il portafoglio in reduce-only: l'agente riceve
  `REDUCE_ONLY` sulle aperture finche' l'admin non riprende con il TOTP.

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

- `run_backtest`: mette in coda un backtest e risponde `{backtest_id, status: "queued"}`. Parametri: `strategy` (`sma_cross` con `fast=20`, `slow=50`; `rsi_reversion` con `period=14`, `buy_below=30`, `sell_above=70` e filtro di tendenza facoltativo `sma_filter=n` (compra solo con chiusura sopra la SMA n); `breakout` con `lookback=20`, `exit_lookback=10`; `momentum` con `lookback=63`, `skip=0`, `enter_above=0`, `exit_below=0` (ROC in % della chiusura di `skip` barre fa rispetto a `lookback` barre prima: compra sopra `enter_above`, vende sotto `exit_below`, `exit_below` <= `enter_above`, valori da -100 a 1000); `donchian` con `entry=20`, `exit=10` (compra sopra il massimo delle `entry` barre precedenti, vende sotto il minimo delle `exit` precedenti) e stop facoltativo `atr_stop=k` (vende anche sotto il massimo delle ultime `entry` barre meno k × ATR); `buy_and_hold`; tutte tranne `buy_and_hold` accettano il filtro di volatilita' `max_atr_pct` (salta gli ingressi con ATR sopra quella % della chiusura) e `atr_period` (default 14); i filtri facoltativi valgono 0 = spento), `symbols`, `from_date`, `to_date`, `timeframe` (default `1d`), `initial_cash` (default `10000`, diviso in parti uguali tra i simboli), `params`, `portfolio_id` (opzionale, serve `read`: ne usa le commissioni e lo slippage, altrimenti la regola globale), `slippage_bps` (opzionale; precedenza: parametro, portafoglio, 5 bps). La commissione e' risolta per ogni simbolo (classe di attivita'); la parte fissa e i limiti min/max sono convertiti dalla valuta della regola a quella dello strumento al cambio dell'inizio del backtest, e `fx_pct` si somma alla percentuale per le operazioni in valuta diversa; senza cambio disponibile resta la regola cosi' com'e' con il warning `fee_currency_unconverted:EUR->USD`. Con un periodo inferiore a un anno il CAGR e' annualizzato da un periodo breve (warning `cagr_annualised_from_short_span`). I segnali si eseguono all'apertura della barra successiva con slippage. Al massimo 2 backtest in coda o in esecuzione per agente (`RATE_LIMITED`), al massimo 5 anni di barre giornaliere o 90 giorni di barre intraday (`RISK_LIMIT_EXCEEDED`).
- `get_backtest`: stato e risultato di un backtest dell'agente (di un altro agente: `FORBIDDEN`). Il `result` contiene `metrics` (`total_return_pct`, `cagr_pct`, `max_drawdown_pct`, `sharpe`, `win_rate_pct`, `trades`, `exposure_pct`), `equity` (al massimo 1000 punti) e `trades`; un backtest fallito ha `error`.
- `list_backtests`: i backtest dell'agente, dal piu' recente, senza curva e operazioni; filtro `status`, `limit` 1-100.

### Blocco aperture (CEO)

- `set_trading_halt`: attiva (`active: true`) o revoca (`active: false`) il blocco delle nuove aperture. Serve il permesso `halt` (di solito solo il CEO; senza: `FORBIDDEN` con `details.needed = "halt"`). `scope` `global` (tutti i portafogli, default) o `portfolio` (con `portfolio_id`, serve `read` su quel portafoglio); `reason` 1-500 caratteri, obbligatoria per attivare (i caratteri di controllo sono rimossi). Risponde `{active, changed, halt}` (attivando anche `canceled`: quanti ordini in attesa che aprirebbero esposizione sono stati annullati): la stessa motivazione una seconda volta non cambia nulla (`changed: false`), una motivazione diversa sostituisce il blocco (`halt.set` con `replaces`, «Motivo aggiornato»); revocare senza blocco attivo da' `{active: false, changed: false, halt: null}`. Un blocco impostato dagli amministratori lo revocano (o modificano) solo loro: `FORBIDDEN` «Blocco impostato dagli amministratori: solo loro possono revocarlo». SMTrading invia da se' il messaggio Telegram critico (`halt.set` / `halt.cleared`, `already_sent` nel feed): non ripeterlo.
- `get_trading_halt`: aperto a tutti gli agenti. I blocchi attivi che coprono i portafogli dell'agente (o solo `portfolio_id`, serve `read`): `{halted, halts: [{id, scope, portfolio_id, portfolio, reason, set_by, since}]}`, dal piu' recente; `set_by` e' il nome dell'agente o `system` (un blocco di `system` non si revoca ne' si modifica: `FORBIDDEN`).

Mentre un blocco copre un portafoglio, ogni ordine che aumenterebbe l'esposizione (nuove posizioni, incrementi, aperture short, ingresso di bracket e OCO) e' rifiutato con `TRADING_HALTED` e messaggio `Aperture sospese dal CEO: <motivo>`; il rifiuto resta registrato come ordine `rejected`. Restano permessi gli ordini che riducono o chiudono (`close_position`, vendite fino alla quantita' posseduta), gli stop loss / take profit delle posizioni esistenti, gli annullamenti, le liquidazioni e la sincronizzazione col broker. All'attivazione gli ordini di apertura gia' in attesa (limit, stop, ingressi di bracket con le loro uscite) dei portafogli coperti sono annullati (presso il broker per gli ordini reali); restano gli ordini reduce-only, le uscite delle posizioni esistenti (stop loss, take profit, OCO di chiusura) e le chiusure. Un'apertura sfuggita all'annullamento e' comunque rifiutata quando scatterebbe o prima dell'invio al broker. Il controllo e' quello del kill switch (che ha la precedenza) e vale per portafogli simulati, paper e reali. Il permesso si concede dalla scheda dell'agente o con `python -m smtrading.cli agent halt-permission NOME on`.

### Obiettivi

- `get_goals`: nessun parametro; aperto a ogni agente con almeno un portafoglio (`read` o `trade`; senza portafogli: `FORBIDDEN`). Gli obiettivi attivi fissati in SMTrading (pagina «Obiettivi») con l'avanzamento nel periodo in corso: `{goals: [{name, horizon, scope, portfolio_id, portfolio_name, kind, period_start, period_end, start_eur, current_eur, target_eur, pct, target_pct, gap_eur, elapsed_pct, status, as_of, benchmark, benchmark_pct, diff_pp, loss_pct}]}`. Solo gli obiettivi sul totale (`scope` `total`, `portfolio_id` null) e quelli dei portafogli dell'agente: mai quelli di altri portafogli; gli obiettivi che non si ripetono escono una volta concluso il loro periodo. Un obiettivo sul totale somma tutti i portafogli attivi: i suoi importi in EUR (`start_eur`, `current_eur`, `target_eur`, `gap_eur`) ci sono solo se l'agente vede tutti i portafogli attivi, altrimenti sono null (restano `pct`, `target_pct`, `elapsed_pct`, `status` e i campi dell'indice e della perdita). `horizon` `week` (lunedi'-domenica), `month`, `quarter`, `year` (periodi di calendario, Europe/Rome) o `custom` (date fissate); `kind` `min` (un minimo da superare), `cap` (un tetto da non superare), `max_loss` (una perdita da non raggiungere: `target_pct` e' il limite, es. "5" = al massimo -5% nel periodo; `target_eur` e' la soglia minima di equity; `loss_pct` = la perdita attuale del periodo, dall'inizio a ora, 0 se in guadagno: non la peggiore toccata, e lo stato `max_loss` segue questa perdita attuale) o `beat_index` (battere l'indice `benchmark` - `SPY`, `QQQ` o `VWCE.DE` - di `target_pct` punti, anche 0; `benchmark_pct` = la variazione dell'indice nel periodo in EUR, `diff_pp` = `pct` - `benchmark_pct`; `target_eur` = l'equity che pareggerebbe l'indice piu' il margine); `benchmark`, `benchmark_pct`, `diff_pp` null per gli altri tipi, `loss_pct` null salvo `max_loss`; `period_start`/`period_end` `YYYY-MM-DD` (fine inclusa); importi in EUR e percentuali come stringhe, null finche' non ci sono dati del periodo; `gap_eur` = `target_eur` - `current_eur`; `elapsed_pct` = la parte del periodo trascorsa in giorni di borsa (0-100); `status` `raggiunto`, `in linea`, `in ritardo` (sotto la quota proporzionale al tempo trascorso, oltre mezzo punto), `oltre il tetto` (solo `cap`), `nel limite` / `vicino al limite` (perdita da 80% del limite) / `superato` (perdita pari o oltre il limite) (solo `max_loss`), `avanti` (scarto pari o oltre il margine) / `in linea` / `indietro` (sotto il margine di oltre mezzo punto) (solo `beat_index`; `non iniziato` finche' manca il dato dell'indice), `non iniziato`, `concluso`. Ordine: orizzonte (settimana, mese, trimestre, anno, personalizzato), poi totale prima dei portafogli, poi nome. I limiti di rischio vengono sempre prima dell'obiettivo. Quando un obiettivo minimo passa «in ritardo» SMTrading apre da se' un task «OBIETTIVO …» al CEO; quando un obiettivo `max_loss` e' «vicino al limite» o «superato» apre «RISCHIO OBIETTIVO …» al Risk Manager e al CEO: nessun blocco automatico, decidono gli agenti.

### Decisioni, feed delle attivita' e Telegram

- `log_decision`: registra una decisione nello storico permanente (visibile nella web app): `kind` (`proposal`, `approval`, `veto`, `allocation`, `strategy`, `risk_rule`, `other`, `lesson`, `team_rule`), `summary` (1-500 caratteri), `portfolio_id` facoltativo (serve `read`), `symbols` (al massimo 50, max 32 caratteri l'uno, portati in maiuscolo e senza duplicati), `details` (oggetto, al massimo 8 KB). Tre campi facoltativi, da compilare sempre: `outcome` (`approvata`, `respinta`, `sospesa`, `modificata` o `informativa`), `subject` (cosa si decide, in chiaro, al massimo 200 caratteri, es. «incrocio medie mobili 10/30 su SPY, QQQ e DIA») e `reason` (perche', al massimo 300 caratteri). Risponde `{decision_id, created_at}`; non invia messaggi Telegram. `lesson` (errore o perdita evitabile) con `details` `{what_happened, why, next_time, agents: [ruoli], refs: [id decisione o task]}`; `team_rule` (regola del team nata dalle lezioni) con `details` `{rule, applies_to: [ruoli], from_lesson_ids, review_by, status: active|retired}`: una regola si ritira con una nuova `team_rule` con `status` `retired`, e non supera mai i limiti del profilo di rischio, il kill switch o un blocco.
- `get_decisions`: le decisioni visibili all'agente, dalla piu' recente, filtrabili per `portfolio_id` (serve `read`, altrimenti `FORBIDDEN`), `agent` (nome o id; un agente sconosciuto da' `[]`), `kind` (es. `team_rule` per le regole del team, `lesson` per le lezioni), `since`; `limit` 1-200 (default 50). Ogni decisione ha anche `outcome`, `subject` e `reason` (`null` se non dati).
- `get_activity_feed`: il feed delle attivita' (ordini, fill, eventi di rischio, alert, decisioni) dei portafogli dell'agente e globali: `{items, next_after_id}`; `limit` 1-200. Vedi sotto il contratto del cursore.
- `send_telegram`: invia un messaggio al gruppo Telegram (serve il permesso `notify`, altrimenti `FORBIDDEN`): `text` inviato cosi' com'e', senza formattazione; `level` `info` (nessun prefisso), `warning` (🟠) o `critical` (🔴). Limiti e errori nella sezione «Notifiche Telegram».

#### Decisioni

Ogni agente registra con `log_decision` ogni decisione presa in Paperclip (proposte,
approvazioni, veti, allocazioni, strategie, regole di rischio, altro) e poi coinvolge il
Notifier con un task o una menzione: il Notifier non fa polling. Le decisioni restano nello
storico (web app, *Notifiche*) e compaiono nel feed e nei riepiloghi: il riepilogo delle
18:00 ha la sezione «🧭 Decisioni di oggi» (il settimanale «della settimana»), raggruppata
per tipo, una riga per decisione «✅/❌/⏸ <subject> — <portafoglio> — <reason>» con
l'agente; senza `subject`/`reason` (agente non aggiornato o decisione vecchia) si usa
`summary` accorciato. Al massimo 15 righe, poi «… e altre N». `portfolio_id`
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
- `MARKET_CLOSED_QUEUED`: codice riservato, oggi non restituito. A mercato chiuso un ordine
  a mercato **non** e' un errore: `place_order` risponde con l'ordine in stato `queued`, che
  parte all'apertura (gli ordini limit/stop restano `pending`).
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
- `PROVIDER_UNAVAILABLE`: dati di mercato non disponibili, oppure ordine rifiutato dal broker
  (`reject_code`, con il messaggio del broker o `"rejected by the broker"`).
- `LIVE_NOT_ARMED`: portafoglio reale con il denaro reale non attivo (collegamento
  disattivato o sospeso, anche tra il controllo e l'invio): l'ordine non e' stato inviato.
- `LIVE_DRY_RUN`: ordine fermato dalla prova a secco, non inviato al broker. Solo con
  `mcp_hide_mode` spento: con la modalita' nascosta (default) l'agente vede un normale
  rifiuto del broker (`PROVIDER_UNAVAILABLE`, `"rejected by the broker"`).
- `LIVE_CASH_LIMIT`: acquisto su un portafoglio reale oltre il tetto di liquidita': stima +
  commissioni + margine del 2% devono stare entro la liquidita' del portafoglio e quella
  disponibile al broker (convertita nella valuta del portafoglio), meno gli acquisti aperti.
- `INTERNAL`: errore interno inatteso. Non fa parte dei codici della spec: il messaggio e'
  sempre generico (`"internal error"`), il dettaglio e' solo nei log del server e la chiamata
  e' registrata nell'audit con esito `INTERNAL`. Ripetere piu' tardi o segnalarlo.

Altri codici del ledger (`INSUFFICIENT_FUNDS`, `INVALID_AMOUNT`, ...) riguardano operazioni
non esposte agli agenti.
