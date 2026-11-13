# SMTrading MCP per Paperclip

SMTrading espone un server MCP (nome `SMTrading`) via HTTP streamable su `/mcp`.
Ogni agente ha un token personale; i permessi sono per portafoglio (`read` o `trade`).

## 1. Recuperare il token

Il token in chiaro viene mostrato **una sola volta**, al primo avvio, quando il servizio
`migrate` esegue il seed (agente `paperclip-demo`, permesso `trade` sul portafoglio "Demo"):

```bash
docker compose logs migrate | grep "token agente"
```

Formato: `smt_<prefix>_<secret>`. Se il log non e' piu' disponibile, il token va ruotato dalla
web app di amministrazione (piano 5): il vecchio token smette di funzionare.

## 2. Collegare Paperclip alla rete

```bash
scripts/connect-paperclip.sh            # container "paperclip" (default)
scripts/connect-paperclip.sh altro-nome
```

Lo script esegue `docker network connect smtrading-net <container>` ed e' idempotente:
se il container e' gia' sulla rete non fa nulla.

## 3. Configurare il server MCP in Paperclip

Server MCP di tipo HTTP:

- URL: `http://api:8000/mcp` (oppure `http://smtrading-api-1:8000/mcp`)
- Header: `Authorization: Bearer <token>`

Esempio di configurazione:

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

## 4. Metodi

Passa i numeri come stringhe per mantenere la precisione esatta (es. `"qty": "0.1"`).
Gli agenti non possono creare portafogli, depositare, prelevare o modificare i limiti.

- `list_portfolios`: i portafogli utilizzabili dall'agente, con il permesso (`read`/`trade`).
- `get_portfolio`: valutazione mark-to-market (EUR): equity, esposizione, margini, potere d'acquisto, posizioni, P&L del giorno.
- `get_positions`: posizioni aperte con prezzo medio, mark e P&L non realizzato.
- `get_transactions`: movimenti del ledger del portafoglio, dal piu' recente, con filtri e paginazione (`before_seq`).
- `get_performance`: serie dell'equity e statistiche (rendimento, max drawdown, sharpe) su un periodo.
- `get_risk_limits`: stato di rischio: soglie di margine, profilo effettivo, perdita giornaliera, kill switch.
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
- `search_instruments`: cerca strumenti per simbolo o nome (catalogo locale e provider).
- `get_instrument`: anagrafica di uno strumento e stato del suo mercato.
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
- `get_triggered_alerts`: notifiche dei portafogli dell'agente (o indirizzate a lui), dalla piu' recente; filtro `since`, `limit` 1-500.

## 5. Errori

Un errore e' restituito con `isError: true` e un testo JSON
`{"code": "...", "message": "...", "details": {...}}`. Codici:

- `INSUFFICIENT_BUYING_POWER`: potere d'acquisto insufficiente.
- `MARKET_CLOSED_QUEUED`: mercato chiuso, ordine messo in coda.
- `SYMBOL_NOT_ALLOWED`: simbolo non consentito dal profilo di rischio.
- `SYMBOL_NOT_FOUND`: simbolo sconosciuto.
- `KILL_SWITCH_ACTIVE`: kill switch globale attivo.
- `REDUCE_ONLY`: il portafoglio accetta solo ordini che riducono le posizioni.
- `PORTFOLIO_LOCKED`: portafoglio bloccato.
- `STALE_QUOTE`: quotazione troppo vecchia.
- `RISK_LIMIT_EXCEEDED`: limite di rischio superato.
- `SHORT_NOT_ALLOWED`: vendita allo scoperto non consentita.
- `INVALID_ORDER`: argomenti non validi (il campo e' in `details.field`).
- `NOT_FOUND`: risorsa inesistente.
- `FORBIDDEN`: token non valido, portafoglio non assegnato o permesso insufficiente.
- `RATE_LIMITED`: troppe richieste.
- `PROVIDER_UNAVAILABLE`: dati di mercato non disponibili.
- `INTERNAL`: errore interno.

Altri codici del ledger (`INSUFFICIENT_FUNDS`, `INVALID_AMOUNT`, ...) riguardano operazioni
non esposte agli agenti.
