# Istruzioni degli agenti Paperclip per SMTrading

Questa cartella contiene le istruzioni di ruolo, in italiano, dei 9 agenti Paperclip che
lavorano sui 2 portafogli di SMTrading («Portafoglio Prudente» e «Portafoglio Aggressivo»,
500 € ciascuno) tramite MCP. Il collegamento tecnico
(token, rete, server MCP) è descritto in [`../paperclip-mcp.md`](../paperclip-mcp.md).

| File | Agente | Permesso SMTrading | Modalità |
|---|---|---|---|
| `00-regole-comuni.md` | **tutti** (va incollato per primo) | — | — |
| `01-ceo.md` | CEO SMTrading | read + **halt** (unico) | task + routine del venerdì |
| `02-portfolio-manager.md` | Portfolio Manager | read | task + routine oraria |
| `03-risk-manager.md` | Risk Manager | read | task + routine oraria |
| `04-execution-supervisor.md` | Execution Supervisor | **trade** (unico) | solo task |
| `05-market-analyst.md` | Market Analyst | read | solo task |
| `06-news-analyst.md` | News Analyst | read | task + routine (08:30 e ogni 2 ore) |
| `07-quant-researcher.md` | Quant Researcher | read | solo task |
| `08-performance-auditor.md` | Performance Auditor | read | solo task |
| `09-notifier.md` | Notifier | read + **notify** (unico) | solo task |

## 1. Incollare le istruzioni in Paperclip

Per **ogni** agente, nel campo delle istruzioni (instructions / system prompt) dell'agente:

1. incolla **prima** tutto il contenuto di `00-regole-comuni.md`;
2. sotto, una riga vuota, poi tutto il file del ruolo (`NN-<ruolo>.md`);
3. salva. Il nome dell'agente in Paperclip deve corrispondere al ruolo (es. «Risk Manager»):
   gli agenti si cercano a vicenda per nome nei task e in `get_decisions` (filtro `agent`).

Quando cambi una regola comune, aggiorna `00-regole-comuni.md` e reincollalo in **tutti** i 9
agenti, così nessuno lavora con regole vecchie.

## 2. Permessi da configurare

In SMTrading (pagina *Agenti* o CLI, vedi `../paperclip-mcp.md`):

- un token per ogni agente;
- `read` su `Portafoglio Prudente` e `Portafoglio Aggressivo` per tutti; `trade` su entrambi
  **solo** per l'Execution Supervisor;
- `notify` **solo** per il Notifier (`python -m smtrading.cli agent notify Notifier on`);
- permesso **halt** **solo** per il CEO SMTrading (necessario per `set_trading_halt`; si concede
  dalla scheda dell'agente). Un halt attivato da un amministratore nella web app può essere
  revocato solo da un amministratore: al CEO la revoca risponde `FORBIDDEN`.

In Paperclip, per ogni agente, nel connettore SMTrading (*Connectors → SMTrading →
Permissions/Actions*) imposta **Allowed** solo le azioni della sua colonna e **Off** tutte le
altre. Legenda colonne: CEO = CEO SMTrading, PM = Portfolio Manager, RM = Risk Manager, ES =
Execution Supervisor, MA = Market Analyst, NA = News Analyst, QR = Quant Researcher, PA =
Performance Auditor, NOT = Notifier. ✎ = azione di scrittura.

| Azione | CEO | PM | RM | ES | MA | NA | QR | PA | NOT |
|---|---|---|---|---|---|---|---|---|---|
| `list_portfolios` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** |
| `get_portfolio` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | **Allowed** | **Allowed** |
| `get_positions` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | **Allowed** | Off |
| `get_risk_limits` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | **Allowed** | **Allowed** | **Allowed** |
| `get_performance` | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | **Allowed** | **Allowed** | Off |
| `get_transactions` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | **Allowed** | Off |
| `get_trading_halt` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | **Allowed** | Off |
| `get_goals` | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | Off | Off | **Allowed** |
| `list_orders` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | **Allowed** | Off |
| `get_order` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | **Allowed** | Off |
| `preview_order` | Off | **Allowed** | **Allowed** | **Allowed** | Off | Off | Off | Off | Off |
| `place_order` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `place_bracket_order` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `place_oco_order` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `modify_order` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `cancel_order` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `cancel_all_orders` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `close_position` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `close_all_positions` ✎ | Off | Off | Off | **Allowed** | Off | Off | Off | Off | Off |
| `set_trading_halt` ✎ | **Allowed** | Off | Off | Off | Off | Off | Off | Off | Off |
| `search_instruments` | Off | **Allowed** | Off | Off | **Allowed** | **Allowed** | **Allowed** | Off | Off |
| `get_instrument` | Off | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off |
| `get_quote` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off |
| `get_bars` | Off | **Allowed** | **Allowed** | Off | **Allowed** | Off | **Allowed** | Off | Off |
| `get_market_status` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | Off |
| `get_indicators` | Off | **Allowed** | **Allowed** | **Allowed** | **Allowed** | Off | **Allowed** | Off | Off |
| `get_fx_rate` | Off | **Allowed** | **Allowed** | Off | **Allowed** | Off | Off | **Allowed** | Off |
| `get_news` | Off | **Allowed** | **Allowed** | Off | **Allowed** | **Allowed** | Off | Off | Off |
| `get_fundamentals` | Off | **Allowed** | Off | Off | **Allowed** | **Allowed** | **Allowed** | Off | Off |
| `get_earnings_calendar` | Off | **Allowed** | **Allowed** | Off | **Allowed** | **Allowed** | Off | Off | Off |
| `get_dividends` | Off | **Allowed** | Off | Off | **Allowed** | **Allowed** | **Allowed** | Off | Off |
| `screen` | Off | **Allowed** | Off | Off | **Allowed** | Off | **Allowed** | Off | Off |
| `list_watchlists` | Off | **Allowed** | Off | Off | **Allowed** | **Allowed** | Off | Off | Off |
| `create_watchlist` ✎ | Off | **Allowed** | Off | Off | **Allowed** | Off | Off | Off | Off |
| `add_to_watchlist` ✎ | Off | **Allowed** | Off | Off | **Allowed** | Off | Off | Off | Off |
| `remove_from_watchlist` ✎ | Off | **Allowed** | Off | Off | **Allowed** | Off | Off | Off | Off |
| `delete_watchlist` ✎ | Off | **Allowed** | Off | Off | **Allowed** | Off | Off | Off | Off |
| `create_alert` ✎ | Off | **Allowed** | **Allowed** | Off | Off | Off | Off | Off | Off |
| `list_alerts` | Off | **Allowed** | **Allowed** | Off | Off | Off | Off | Off | Off |
| `delete_alert` ✎ | Off | **Allowed** | **Allowed** | Off | Off | Off | Off | Off | Off |
| `get_triggered_alerts` | **Allowed** | **Allowed** | **Allowed** | Off | Off | **Allowed** | Off | Off | Off |
| `run_backtest` ✎ | Off | Off | Off | Off | Off | Off | **Allowed** | Off | Off |
| `get_backtest` | Off | Off | Off | Off | Off | Off | **Allowed** | Off | Off |
| `list_backtests` | Off | Off | Off | Off | Off | Off | **Allowed** | Off | Off |
| `log_decision` ✎ | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** |
| `get_decisions` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** |
| `get_activity_feed` | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** | **Allowed** |
| `send_telegram` ✎ | Off | Off | Off | Off | Off | Off | Off | Off | **Allowed** |

Riepilogo delle azioni di scrittura:

- tool d'ordine (`place_*`, `modify_order`, `cancel_*`, `close_*`): **solo** ES;
- `set_trading_halt`: **solo** CEO;
- `send_telegram`: **solo** Notifier;
- `run_backtest` (e `get_backtest`, `list_backtests`): **solo** QR;
- `create_alert` / `delete_alert` / `list_alerts`: PM e RM (le sentinelle: gli alert arrivano
  solo a chi li crea);
- watchlist (`create_watchlist`, `add_to_watchlist`, `remove_from_watchlist`,
  `delete_watchlist`): PM e MA;
- `log_decision`: tutti gli agenti che decidono o registrano (tutti e 9).

Dopo ogni aggiornamento di SMTrading che aggiunge o cambia metodi (per esempio l'arrivo di
`set_trading_halt`/`get_trading_halt` o di `get_goals`) premi **Refresh actions** nel connettore e ricontrolla
questa tabella: le azioni nuove vanno impostate esplicitamente (Allowed o Off).

Anche se Paperclip lo consentisse per errore, SMTrading rifiuta con `FORBIDDEN` gli ordini di
chi ha solo `read` e i `send_telegram` di chi non ha `notify`.

## 3. Routine (cicli programmati)

SMTrading non può svegliare gli agenti: gli alert di prezzo restano in attesa finché qualcuno
chiama `get_triggered_alerts`. Per questo solo 4 agenti hanno una Routine; gli altri lavorano
solo su task. Fuso orario delle Routine: **Europe/Rome**.

| Agente | Routine (titolo) | Cron (Europe/Rome) | Prompt della Routine |
|---|---|---|---|
| Risk Manager | «Sentinella rischio (oraria)»: ogni ora, lun-ven, 09:00-22:00 | `0 9-22 * * 1-5` | «Esegui il ciclo programmato (sentinella rischio) delle tue istruzioni.» |
| Risk Manager | «Sentinella rischio (weekend, crypto)»: sab-dom 10, 13, 16, 19, 22 | `0 10-22/3 * * 0,6` | «Esegui il ciclo programmato (sentinella rischio) delle tue istruzioni. È fine settimana: sono aperti solo i mercati crypto.» |
| Portfolio Manager | «Sentinella portafogli (oraria)»: ogni ora, lun-ven, 09:05-21:05, più 15:35 | `5 9-21 * * 1-5` e `35 15 * * 1-5` | «Esegui il ciclo programmato (sentinella) delle tue istruzioni.» |
| Portfolio Manager | «Sentinella portafogli (weekend, crypto)»: sab-dom 10:05, 13:05, 16:05, 19:05, 22:05 | `5 10-22/3 * * 0,6` | «Esegui il ciclo programmato (sentinella) delle tue istruzioni. È fine settimana: sono aperti solo i mercati crypto.» |
| Portfolio Manager | «Chiusura USA (serale)»: lun-ven 22:15 | `15 22 * * 1-5` | «Esegui il ciclo «Chiusura USA (serale)» delle tue istruzioni.» |
| News Analyst | «Scansione pre-mercato 08:30» | `30 8 * * 1-5` | «Esegui la scansione pre-mercato delle 08:30.» |
| News Analyst | «Scansione notizie (ogni 2 ore)»: 10:00-22:00 | `0 10-22/2 * * 1-5` | «Esegui la scansione rapida.» |
| CEO SMTrading | «Richiesta audit settimanale»: venerdì 16:00 | `0 16 * * 5` | «Crea il task AUDIT SETTIMANALE per il Performance Auditor.» |
| CEO SMTrading | «Revisione settimanale»: venerdì 17:00 | `0 17 * * 5` | «Esegui la revisione settimanale.» |

Consigli:

- crea una Routine per riga; assegnala all'agente indicato; disattiva la sovrapposizione
  (se un ciclo è ancora in corso, il successivo va saltato, non accodato);
- il RM gira al minuto 00 e il PM al minuto 05: il PM trova lo stato di rischio aggiornato;
- il venerdì alle 16:00 il CEO chiede l'audit al Performance Auditor, così alle 17:00 lo trova pronto;
- ogni ciclo parte dal feed e dagli alert e si ferma subito se non è cambiato nulla, quindi il
  costo di un'ora tranquilla è di poche chiamate;
- ogni Routine apre un task all'agente: l'agente lo chiude a fine ciclo (anche «Nessuna
  novità»); un task rimasto aperto fa saltare i cicli successivi;
- chi passa il lavoro a chi, con quale task e titolo: [`HANDOFFS.md`](HANDOFFS.md) (non va
  incollato negli agenti).

## 4. Come parte la catena da un task dell'utente

Il modo principale di lavorare è il task: l'utente (o il CEO) crea un task e gli agenti si
passano sotto-task fino al risultato. Esempio di primo avvio, da assegnare al **CEO SMTrading**:

```text
Titolo: Avvio trading su Portafoglio Prudente e Portafoglio Aggressivo
Assegnatario: CEO SMTrading
Descrizione:
Obiettivo: avviare i 2 portafogli con uno stile coerente col profilo e confrontarne i risultati.
1. Chiedi al Quant Researcher almeno una strategia backtestata per portafoglio
   (PRU lungo/medio termine su ETF e large cap; AGG swing + intraday).
2. Approva solo strategie con backtest ≥ 1 anno di barre giornaliere.
3. Chiedi al Portfolio Manager un'allocazione per ciascun portafoglio e approvala.
4. Quando le allocazioni sono approvate, il PM può proporre i primi trade (flusso PM → RM → ES).
5. Riportami entro venerdì: strategie approvate, allocazioni, prime operazioni.
```

Cosa succede dopo (senza altri interventi):

1. il CEO crea 2 task «RICERCA STRATEGIA» per il Quant Researcher (uno per portafoglio);
2. il Quant esegue `run_backtest`, registra i risultati con `log_decision` e crea i task
   «APPROVA STRATEGIA» per il CEO;
3. il CEO approva o respinge (`log_decision` `strategy`) e, se approva, crea «STRATEGIA
   APPROVATA» per il PM;
4. il PM propone le allocazioni (task «ALLOCAZIONE»), il CEO le approva (`allocation`);
5. dalla Routine oraria il PM trova i segnali (o chiede un'«ANALISI» al Market Analyst), crea
   le «PROPOSTE» per il RM;
6. il RM approva (`approval`) e crea «ESEGUI» per l'Execution Supervisor, oppure mette il veto
   e crea «VETO» per il PM;
7. l'ES fa `preview_order` e `place_bracket_order`, risponde con l'esito;
8. SMTrading invia da sé su Telegram ordini eseguiti/rifiutati e i riepiloghi delle 18:00 e del
   venerdì 18:30; il Notifier interviene solo per escalation e decisioni da comunicare.

Altri task utili da creare a mano:

- al **CEO**: «Revisione straordinaria: AGG ha perso il 6% questa settimana, valuta un halt»;
- al **Performance Auditor**: «AUDIT GIORNALIERO di oggi»;
- al **Quant Researcher**: «VALUTA IDEA: rsi_reversion su BTC-USD ed ETH-USD per PRU, 3 anni 1d»;
- al **Notifier**: «NOTIFICA info: comunica che da lunedì AGG è sospeso per manutenzione strategie».

## 5. Note

- Portafogli attivi (sostituiscono i 4 «Test 250», archiviati):

  | Codice | Nome | Profilo | Capitale | Ordine max | Posizione max | Perdita giorn. max | Escalation 2/3 | Mercati | Short | Stile |
  |---|---|---|---|---|---|---|---|---|---|---|
  | PRU | `Portafoglio Prudente` | 2 · Prudente | 500 € | 1500 € | 15% (≈ 75 €) | 3% (≈ 15 €) | ≈ 10 € | azioni/ETF USA+UE, crypto | no | lungo/medio |
  | AGG | `Portafoglio Aggressivo` | 4 · Aggressivo | 500 € | 5000 € | 40% (≈ 200 €) | 8% (≈ 40 €) | ≈ 26,67 € | tutti | solo «short tattico» | swing + intraday |

  Costi: commissione 0, 0,15% sul cambio valuta, slippage circa 5 bp (nei testi degli agenti:
  «costi di esecuzione»).
- **Cosa sanno gli agenti.** Le istruzioni presentano i 2 portafogli come portafogli reali
  presso il broker gestiti tramite SMTrading: non contengono alcun riferimento a simulazione,
  denaro virtuale o test. Finché l'impostazione `mcp_hide_mode` è attiva, i tool MCP non
  espongono agli agenti il campo `mode` (né `live_state`) dei portafogli che non sono
  collegati a un conto reale armato. Ad oggi i 2 portafogli non hanno un collegamento broker:
  gli ordini vengono eseguiti dal motore interno di SMTrading con prezzi reali e nessun ordine
  raggiunge un broker. Gli agenti non vengono informati di questo.
- Gli obiettivi (settimana, mese, trimestre, anno o date fissate; sul totale o su un
  portafoglio) non sono nelle istruzioni: si fissano nella pagina «Obiettivi» di SMTrading e
  gli agenti li leggono con `get_goals` (ognuno vede quelli sul totale e quelli dei suoi
  portafogli). Il CEO li divide in tappe settimanali.
- I riepiloghi automatici (07:00, 18:00, venerdì 18:30), il controllo pre-apertura (lun-ven
  08:45), gli avvisi sugli obiettivi e sulle posizioni senza protezione e gli avvisi su ordini
  eseguiti/rifiutati e rischi critici li invia SMTrading: il Notifier non li ripete.
- Lo storico di tutte le decisioni (`log_decision`) è visibile nella web app di SMTrading,
  pagina *Notifiche*.
