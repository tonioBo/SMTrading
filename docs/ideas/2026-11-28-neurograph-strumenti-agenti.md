# Neurograph: altri strumenti per gli agenti — piano di ricerca

Data: 28/11/2026 · Stato: idea, da decidere (Matteo: «ci penso, solo il piano»). Nulla è costruito.

## Principio

Neurograph **prima verifica sullo storico** che uno strumento batta un metodo semplice di confronto, sui dati
successivi al periodo di studio (come il test T0, dove GARCH(1,1)-t ha battuto i modelli di previsione più
complessi su 209 simboli). Solo se lo supera diventa uno strumento MCP di SMTrading, alle stesse condizioni di
`get_forecast`: sola lettura, misura e mai ordine, «non disponibile» senza bloccare nulla, istruzioni
generiche agli agenti, messa in linea ad agenti fermi, aggiornamento del Fiume dei flussi. Durante la campagna
al massimo uno strumento nuovo (ogni strumento cambia l'elenco degli agenti).

## Dove gli agenti oggi faticano

- Stop in percentuale fissa che ignorano il rumore normale del titolo (stop MSFT; regole di prova del 27/11
  fermate tutte dopo 3 perdite).
- Rischio guardato posizione per posizione, non sul portafoglio.
- Nessun criterio oggettivo per scegliere il tipo di strategia adatto al mercato di adesso (revisione del Quant
  del 27/11: solo conferme).

## Gli strumenti, in ordine di valore

| # | Strumento | Cosa risponde | Chi lo usa | Test che deve superare | Cosa serve a Neurograph |
|---|---|---|---|---|---|
| 1 | `get_bracket_odds` | Probabilità di toccare prima lo stop o l'obiettivo entro l'orizzonte, risultato atteso netto dei costi | Risk Manager, Portfolio Manager, regole automatiche | Calibrazione: dove dice 30 %, succede ~30 % sui dati successivi (per fasce di probabilità), confronto con il metodo «distanza / ATR» | Simulazione dei percorsi dal GARCH-t già in uso; prezzi max/min per barra per contare i tocchi |
| 2 | `get_portfolio_risk` | Correlazioni tra le posizioni, perdita possibile del portafoglio in una giornata storta (VaR/ES 95 %), con e senza l'ordine proposto | Risk Manager | Test di Kupiec e Christoffersen sui giorni oltre la stima; confronto con correlazioni storiche semplici | Covarianze dinamiche (DCC o restringimento), più serie insieme |
| 3 | `get_regime` | Volatilità alta/normale/bassa rispetto alla storia del titolo; tendenza al trend o al ritorno (rapporto delle varianze / Hurst) | Quant, CEO | Le strategie scelte in base al regime rendono più di quelle scelte senza, sui dati successivi, al netto dei costi | Statistiche per finestra mobile; eventuale modello a stati |
| 4 | Parametri delle regole automatiche in base alla volatilità | Stop e obiettivo come multipli del movimento tipico al minuto | Quant (regole del modo B) | Riprova degli ultimi giorni: risultato netto migliore delle percentuali fisse | Volatilità su barre da 1 minuto (dopo il 23/12, con l'intraday) |
| 5 | Rischio attorno alle trimestrali | Forchetta allargata nei giorni di trimestrale, dai movimenti passati | Risk Manager, Market Analyst | La forchetta allargata contiene il movimento reale più spesso di quella normale | Date delle trimestrali passate (SMTrading ha il calendario) |

## Cosa non fare

- Previsioni di direzione: il T0 mostra che non battono i metodi semplici e spingerebbero a operare su
  un'indicazione debole.
- Classifiche dei titoli «migliori» come segnale per il Portfolio Manager (al più materiale di ricerca per il
  Quant, dopo un test severo).
- Lettura delle notizie: la fa già il News Analyst.

## Note

- Il codice di Neurograph è sull'altro PC e non è stato visto: la fattibilità della colonna «cosa serve» è una
  stima.
- La chiamata resta sempre da SMTrading verso Neurograph (barre di SMTrading, VPN); l'MCP resta chiuso.
