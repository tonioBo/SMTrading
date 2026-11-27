# Manuale della prova reale dei due modi del lavoro in giornata (conto Alpaca di prova da 30.000 $)

Solo documentazione: nessun codice. Si esegue quando c'è una seduta USA piena e gli agenti sono tranquilli. L'esito si annota nella riga della sezione 3 di `2026-12-campagna-prontezza.md` («fatta: …») e nella pagella; a fine prova un messaggio Telegram con l'esito.

## Dati di partenza

- Collegamento broker già esistente: «Alpaca Intraday e Scraping», id `76bb07ff-353c-427d-8020-0c2ac50c7f05`, tipo `alpaca_paper`, patrimonio 30.000 $, moltiplicatore 2, vuoto, cripto abilitate. **Non scrivere mai chiavi o segreti** in questo documento, nei messaggi o nei registri. Non è il collegamento del Portafoglio Alpaca della campagna.
- La regola PDT non esiste più (abolita il 4/6/2026): la prova verifica il **tetto proprio** `max_day_trades_5d`, la **leva di giornata** (leva del portafoglio 2, solo in prova) e i **rifiuti di potere d'acquisto** del broker. Nessuna regola dei 25.000 $, nessun confronto con `daytrade_count`.
- Un modo per portafoglio: due portafogli di prova **nascosti** sul collegamento (Prova A modo A, Prova B modo B), `in_totals=false`, nessun accesso agli agenti (nessuna riga `agent_portfolios`), fuori da ogni campagna.
- Prova A non ha agenti: gli scatti non svegliano nessuno e producono l'esito su Telegram («system»).
- Passi sulle azioni: durante la seduta USA (15:30-22:00 ora italiana). Cripto: a qualsiasi ora. Prezzi dal flusso gratuito IEX: se ne misura lo scarto (passo 13).

## Limiti noti da tenere presenti

1. Mai ordini di vendita manuali «solo riduzione» su un simbolo di una regola automatica attiva: finding aperto, il resto della posizione resterebbe senza stop. Per chiudere a mano si mette prima in pausa la regola (passo 10 e pulizia).
2. Il replay a minuti ha limiti (profondità dello storico gratuito, buchi): annotare quelli trovati al passo 6.
3. Da verificare dal vivo che Alpaca accetti il canale barre entro il limite gratuito di 30 simboli (passo 5).
4. La migrazione crea l'indice su `orders` senza `CONCURRENTLY`: messa in linea **solo con agenti e worker fermi**. Questo manuale non prevede nessun deploy.

## Passi

Per ogni passo: azione, controllo atteso, cosa annotare nel documento di prontezza.

1. **Prerequisiti.**
   - Azione: giorno di seduta USA piena (non mezza giornata); agenti Paperclip tranquilli; nessuna messa in linea in corso; annotare lo stato del flusso: `python -m smtrading.marketdata.cli status`.
   - Atteso: stato del flusso salvato (simboli, ordini e posizioni nello stream).
   - Annotare: data/ora, stato del flusso iniziale (servirà ai passi 5 e 14).

2. **Controllo del collegamento.**
   - Azione: dal portale verificare «Alpaca Intraday e Scraping» con il test in sola lettura; `GET /api/intraday/accounts`. Conto e posizioni si leggono sempre dal portale (questa risposta e la pagina Posizioni), mai con le chiavi dalla shell.
   - Atteso: conto letto con `buying_power`, `multiplier` 2, `margin: true`, `trading_blocked` e `account_blocked` falsi; `fresh: true` dopo aver acceso un interruttore (passo 3).
   - Annotare: valori letti (senza chiavi).

3. **Due portafogli di prova nascosti** (`POST /api/portfolios`, poi `PATCH /api/portfolios/{id}`), sullo stesso collegamento.
   - Comune: `in_totals=false`, nessuna riga `agent_portfolios`, `leverage=2` (provare anche il rifiuto: il `PATCH` che accende il modo, con `leverage=2.5`, deve essere rifiutato dal portale; annotarlo).
   - **Prova A**: `intraday_enabled=true`, `intraday_timeframes=["15m"]`, `max_day_trades_5d=1`.
   - **Prova B**: `auto_rules_enabled=true` (il portale accetta: conto con margine, non bloccato), `max_day_trades_5d=2`, freni di portafoglio di default.
   - Atteso: entrambi creati e accesi senza errori; nei totali non compaiono. Annotare gli id.

4. **Modo A: strategia 15m.**
   - Azione: scegliere 1-2 titoli liquidi già seguiti dalla campagna; approvare la strategia per la sola Prova A: `python -m smtrading.cli intraday drill-strategy --portfolio <id|nome> --agent <nome> (qualsiasi agente esistente va bene: la decisione resta invisibile perché Prova A non ha righe `agent_portfolios`) --ref <rif> --type <rsi|incrocio> --params '<json>' --timeframe 15m --symbols A,B`.
   - Atteso: per alcune sedute scatti registrati nella pagina Segnali e messaggi Telegram alle chiusure di barra giuste; nessuno sveglio (Prova A non ha agenti); nessun messaggio a mercato chiuso.
   - Annotare: numero di scatti, orari rispetto alla chiusura di barra, eventuali scatti mancanti o doppi.

5. **Modo B: regole automatiche su Prova B.**
   - Azione: 1-2 regole su titoli liquidi **diversi** da quelli del passo 4 (`breakout`, `vwap_pullback`, importi piccoli) e 1-2 su cripto liquide (`mean_reversion`). Prima di attivare, per ognuna la prova sullo storico dalla pagina «Regole automatiche» (ultimi 5 giorni, `POST /api/auto-rules/backtest` con `from` e `to` nel formato `YYYY-MM-DD`; al massimo 30 giorni e 5 simboli per prova): annotare il risultato.
   - Attivazione (`POST /api/auto-rules/{id}/activate`): la pagina mostra «con flusso»; `python -m smtrading.marketdata.cli status`.
   - Atteso: i simboli della campagna con ordini e posizioni ancora tutti nello stream; barre solo per i simboli delle regole; Alpaca accetta il canale barre entro il limite gratuito di 30 simboli (limite noto 3).
   - Annotare: simboli totali nello stream, eventuali rifiuti del canale.

6. **Profondità dello storico gratuito.**
   - Azione: `python -m smtrading.marketdata.cli bars <SIMBOLO> 15m --days 60` e `... 1m --days 5` su un titolo e una cripto.
   - Atteso: serie lette; annotare prima barra e buchi (limite noto 2).

7. **Misure del modo B per alcune sedute.**
   - Misurare: tempi scatto→invio (obiettivo < 1 s), invio→broker, broker→esecuzione; scarto atteso→eseguito; commissioni vere (confronto con quanto addebita Alpaca; soglia di prontezza: scarto < 1%); risultato netto.
   - Atteso: riepilogo serale su Telegram; confronto con la prova sullo storico degli stessi giorni.
   - Annotare: tabella delle misure e lo scarto rispetto allo storico.

8. **Uscite del modo B.**
   - Azione: (a) uscita a tempo: `max_hold_s` sta dentro `params` (10-3600 s) e la modifica è rifiutata su una regola attiva (crea una nuova versione): `POST /api/auto-rules/{id}/pause`, poi `PATCH /api/auto-rules/{id}` con `{"params": {…tutti i parametri attuali…, "max_hold_s": 60}}`, poi `POST /api/auto-rules/{id}/resume`; (b) uscita di fine seduta: lasciare una posizione aperta apposta vicino alla fine; (c) stop o obiettivo.
   - Atteso: (a) e (b) chiuse dallo `scalper` (la fine seduta 2 minuti prima della rete di sistema); (c) eseguito dalla coppia sintetica di SMTrading.
   - Annotare: orari attesi ed effettivi.

9. **Freni.**
   - Azione: su una regola `max_trades_hour=1`; su un'altra `max_consecutive_losses=1`. I freni si cambiano come i parametri: pausa → `PATCH` con `{"brakes": {…tutti i freni…}}` (sostituiti per intero) → ripresa.
   - Atteso: seconda entrata dell'ora bloccata; dopo una perdita la regola risulta «fermata dai freni» con avviso Telegram; riattivazione manuale con `POST /api/auto-rules/{id}/resume`.
   - Annotare: messaggi e orari.

10. **Proprietario del simbolo.**
    - Azione: con una regola attiva, acquisto manuale sul suo simbolo dal portale; poi chiusura manuale della posizione.
    - Atteso: acquisto rifiutato («gestito da una regola automatica»); la chiusura manuale accettata. Per la chiusura mettere **prima** la regola in pausa (`POST /api/auto-rules/{id}/pause`): niente vendite «solo riduzione» a regola attiva (limite noto 1).
    - Annotare: testi dei rifiuti.

11. **Tetto di giornata, leva e potere d'acquisto.**
    - Azione: su Prova A un'entrata e un'uscita manuale «di giornata» (1 operazione), poi una seconda entrata di giornata. Poi un acquisto «di giornata» con importo oltre il `buying_power` del riquadro (o oltre patrimonio × 2).
    - Atteso: seconda entrata rifiutata con «Tetto operazioni di giornata del portafoglio raggiunto …» (`max_day_trades_5d=1`); una vendita resta sempre possibile; Prova B non toccata dal tetto di Prova A (nessun budget per collegamento); acquisto oltre il limite rifiutato con «oltre il potere d'acquisto del conto» (o «oltre la leva ammessa»); il riquadro Posizioni mostra potere d'acquisto, margine, ora di lettura e «usate 1 su 1» per Prova A.
    - Annotare: i testi esatti dei rifiuti e i valori del riquadro.

12. **Rete di fine seduta.**
    - Azione: su Prova A lasciare apposta una posizione di giornata aperta.
    - Atteso: ordine di sistema `day_close_minutes` prima della fine, messaggio Telegram, posizione chiusa prima della fine; su Prova B la rete non trova nulla (lo `scalper` è uscito 2 minuti prima).
    - Annotare: orario dell'ordine e della chiusura.

13. **Prezzi IEX contro consolidati.**
    - Azione: confrontare l'ultimo prezzo del portale con un grafico pubblico consolidato, e il volume IEX con quello consolidato, su alcuni titoli.
    - Annotare: scarto medio e massimo (per la pagella: «scarto prezzo» entro lo spread tipico, nessun caso > 1% su titoli liquidi).

14. **Nessun disturbo alla campagna.**
    - Atteso: nessun compito Paperclip nuovo; nessun cambio nei portafogli della campagna; catalogo MCP invariato; stato del flusso uguale al passo 1; uscite della campagna nei tempi di sempre.
    - Annotare: sì/no per voce.

15. **Pulizia completa.** Ordine obbligatorio: prima si cancellano i dati di prova, poi si spengono gli interruttori, si scollegano e si archiviano i portafogli; le righe di audit si cancellano per ultime.
    1. Regole in pausa; chiudere le posizioni (a mano, regole già in pausa) e le cripto residue; nessun ordine aperto.
    2. Conto **vuoto**, letto dal portale (`GET /api/intraday/accounts`, pagina Posizioni): nessuna posizione, nessun ordine aperto, nessun residuo. Mai con le chiavi nella shell.
    3. Decisioni di prova: `python -m smtrading.cli intraday drill-revoke --portfolio <Prova A> --agent <lo stesso agente del passo 4> --ref <rif> --purge`.
    4. Regole automatiche, versioni, operazioni, chiavi Redis `autorules:*` e `bars1m:*` dei simboli di prova: `python -m smtrading.cli autorules purge --portfolio <Prova B>` (equivale a `DELETE /api/portfolios/{id}/auto-rules`).
    5. Scatti: `DELETE /api/portfolios/{id}/intraday-triggers` su Prova A.
    6. Spegnere gli interruttori `intraday_enabled` e `auto_rules_enabled` (`PATCH /api/portfolios/{id}`).
    7. **Scollegare i portafogli dal collegamento** (l'archiviazione altrimenti è rifiutata con «Scollega prima il broker»): `PUT /api/portfolios/{id}/broker-link` con `{"broker_link_id": null}`, per Prova A e Prova B. Il collegamento **non** si disattiva. Per un collegamento `alpaca_paper` il codice non vincola lo scollegamento alla storia contabile (blocca solo ordini ancora in corso sul broker e, per i soli collegamenti reali, posizioni aperte): se il portale lo rifiutasse, annotare il testo esatto e fermarsi. Il portafoglio intraday da 3.000 $ degli agenti non viene toccato.
    8. Archiviare entrambi: `POST /api/portfolios/{id}/archive`.
    9. Il collegamento «Alpaca Intraday e Scraping» resta attivo: ospiterà il portafoglio intraday degli agenti.
    10. **Ultimo passo: righe di audit.** Le righe `admin:*` stanno nella tabella `mcp_audit_log`. Verificato nel codice: non è append-only (nessun trigger, a differenza di `live_audit`), nessuno strumento MCP la legge e il guardiano esclude le righe `admin:%`: sono visibili solo agli amministratori, mai agli agenti. Si cancellano comunque quelle dei due portafogli di prova (come per le prove di novembre), con gli id annotati al passo 3:
        `DELETE FROM mcp_audit_log WHERE method LIKE 'admin:%' AND (params::text LIKE '%<id Prova A>%' OR params::text LIKE '%<id Prova B>%');`
        Dopo di questo nessun altro comando amministrativo sui due portafogli (ognuno scrive una riga nuova); se ne serve uno, ripetere la cancellazione. La cancellazione stessa non scrive audit.
    11. Verifiche: `SELECT count(*) FROM mcp_audit_log WHERE params::text LIKE '%<id Prova A>%' OR params::text LIKE '%<id Prova B>%'` → 0; `md:tracked:auto_rules` vuoto; `SELECT count(*) FROM portfolios WHERE (intraday_enabled OR auto_rules_enabled) AND status <> 'archived'` → 0.
    - Annotare: esito nel documento di prontezza e messaggio Telegram con l'esito.
