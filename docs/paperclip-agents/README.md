# Istruzioni degli agenti Paperclip per SMTrading

Questa cartella contiene le istruzioni di ruolo, in italiano, dei 9 agenti Paperclip che
lavorano sui 4 portafogli di simulazione di SMTrading tramite MCP. Il collegamento tecnico
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
- `read` su `Test 250 · Difensivo`, `Test 250 · Prudente`, `Test 250 · Moderato`,
  `Test 250 · Aggressivo` per tutti; `trade` sui 4 **solo** per l'Execution Supervisor;
- `notify` **solo** per il Notifier (`python -m smtrading.cli agent notify Notifier on`);
- permesso **halt** **solo** per il CEO SMTrading (necessario per `set_trading_halt`; si concede
  dalla scheda dell'agente quando la funzione è disponibile nella tua versione di SMTrading).

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
`set_trading_halt`/`get_trading_halt`) premi **Refresh actions** nel connettore e ricontrolla
questa tabella: le azioni nuove vanno impostate esplicitamente (Allowed o Off).

Anche se Paperclip lo consentisse per errore, SMTrading rifiuta con `FORBIDDEN` gli ordini di
chi ha solo `read` e i `send_telegram` di chi non ha `notify`.

## 3. Routine (cicli programmati)

SMTrading non può svegliare gli agenti: gli alert di prezzo restano in attesa finché qualcuno
chiama `get_triggered_alerts`. Per questo solo 4 agenti hanno una Routine; gli altri lavorano
solo su task. Fuso orario delle Routine: **Europe/Rome**.

| Agente | Routine | Cron (Europe/Rome) | Prompt della Routine |
|---|---|---|---|
| Risk Manager | ogni ora, lun-ven, 09:00-22:00 | `0 9-22 * * 1-5` | «Esegui il ciclo programmato (sentinella rischio) delle tue istruzioni.» |
| Portfolio Manager | ogni ora, lun-ven, 09:05-21:05 | `5 9-21 * * 1-5` | «Esegui il ciclo programmato (sentinella) delle tue istruzioni.» |
| News Analyst | pre-mercato 08:30 | `30 8 * * 1-5` | «Esegui la scansione pre-mercato delle 08:30.» |
| News Analyst | ogni 2 ore 10:00-22:00 | `0 10-22/2 * * 1-5` | «Esegui la scansione rapida.» |
| CEO SMTrading | venerdì 16:00 | `0 16 * * 5` | «Crea il task AUDIT SETTIMANALE per il Performance Auditor.» |
| CEO SMTrading | venerdì 17:00 | `0 17 * * 5` | «Esegui la revisione settimanale.» |

Consigli:

- crea una Routine per riga; assegnala all'agente indicato; disattiva la sovrapposizione
  (se un ciclo è ancora in corso, il successivo va saltato, non accodato);
- il RM gira al minuto 00 e il PM al minuto 05: il PM trova lo stato di rischio aggiornato;
- il venerdì alle 16:00 il CEO chiede l'audit al Performance Auditor, così alle 17:00 lo trova pronto;
- ogni ciclo parte dal feed e dagli alert e si ferma subito se non è cambiato nulla, quindi il
  costo di un'ora tranquilla è di poche chiamate.

## 4. Come parte la catena da un task dell'utente

Il modo principale di lavorare è il task: l'utente (o il CEO) crea un task e gli agenti si
passano sotto-task fino al risultato. Esempio di primo avvio, da assegnare al **CEO SMTrading**:

```text
Titolo: Avvio trading sui 4 portafogli Test 250
Assegnatario: CEO SMTrading
Descrizione:
Obiettivo: avviare i 4 portafogli con uno stile coerente col profilo e confrontarne i risultati.
1. Chiedi al Quant Researcher almeno una strategia backtestata per portafoglio
   (DIF/PRU lungo termine su ETF e large cap; MOD swing; AGG swing + intraday).
2. Approva solo strategie con backtest ≥ 1 anno di barre giornaliere.
3. Chiedi al Portfolio Manager un'allocazione per ciascun portafoglio e approvala.
4. Quando le allocazioni sono approvate, il PM può proporre i primi trade (flusso PM → RM → ES).
5. Riportami entro venerdì: strategie approvate, allocazioni, prime operazioni.
```

Cosa succede dopo (senza altri interventi):

1. il CEO crea 4 task «RICERCA STRATEGIA» per il Quant Researcher;
2. il Quant esegue `run_backtest`, registra i risultati con `log_decision` e crea i task
   «APPROVA STRATEGIA» per il CEO;
3. il CEO approva o respinge (`log_decision` `strategy`) e informa il PM;
4. il PM propone le allocazioni (task «ALLOCAZIONE»), il CEO le approva (`allocation`);
5. dalla Routine oraria il PM trova i segnali (o chiede un'«ANALISI» al Market Analyst), crea
   le «PROPOSTE» per il RM;
6. il RM approva (`approval`) e crea «ESEGUI» per l'Execution Supervisor, oppure mette il veto;
7. l'ES fa `preview_order` e `place_bracket_order`, risponde con l'esito;
8. SMTrading invia da sé su Telegram ordini eseguiti/rifiutati e i riepiloghi delle 18:00 e del
   venerdì 18:30; il Notifier interviene solo per escalation e decisioni da comunicare.

Altri task utili da creare a mano:

- al **CEO**: «Revisione straordinaria: AGG ha perso il 6% questa settimana, valuta un halt»;
- al **Performance Auditor**: «AUDIT GIORNALIERO di oggi»;
- al **Quant Researcher**: «TEST IDEA: rsi_reversion su BTC-USD ed ETH-USD per PRU, 3 anni 1d»;
- al **Notifier**: «NOTIFICA info: comunica che da lunedì AGG è sospeso per manutenzione strategie».

## 5. Note

- I portafogli sono in **simulazione** (denaro virtuale, prezzi reali): nessun ordine raggiunge
  un broker.
- I riepiloghi automatici (07:00, 18:00, venerdì 18:30) e gli avvisi su ordini eseguiti/
  rifiutati e rischi critici li invia SMTrading: il Notifier non li ripete.
- Lo storico di tutte le decisioni (`log_decision`) è visibile nella web app di SMTrading,
  pagina *Notifiche*.
