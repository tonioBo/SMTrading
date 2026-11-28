# Roadmap nel portale — progetto

Data: 28/11/2026 · Richiesta di Matteo · Stato: da approvare

## Perché

Matteo e Luca devono capire in un colpo d'occhio la strada di SMTrading: da dove si è partiti, dove si è adesso,
cosa viene dopo e come si sta andando, con spiegazioni semplici (anche per chi non è del settore) ma con il
dettaglio vero (lavori, strumenti, esperimenti, numeri reali dei portafogli). La pagina deve restare sempre
aggiornata.

## Lettori e accesso

Matteo e Luca (decisione di Matteo). Luca ha già un account nel portale: la pagina è leggibile da **tutti gli
utenti del portale**, amministratori e sola lettura (rotta GET con l'accesso normale, non solo amministratori).
Nessuna azione dalla pagina.

## Strada scelta (A, decisa da Matteo)

| | Come | Pro | Contro |
|---|---|---|---|
| **A · file curato + numeri vivi (scelta)** | Le tappe e le spiegazioni stanno in un file del codice; Claude lo aggiorna in ogni lavoro; un controllo automatico fallisce se un progetto non è citato; i numeri sono letti dal vivo | Testi chiari; non resta indietro | Le parti scritte cambiano con i lavori, non da sole |
| B · modificabile dal portale | Tappe nel database, modificabili da Matteo e Luca | Modifiche dirette | Più lavoro; testi a rischio di restare indietro |
| C · generata dai documenti | Composta da progetti, piani, prontezza | Zero manutenzione | Elenco tecnico poco leggibile |

## Cosa mostra

1. **In testa: «dove siamo» in una frase** e pochi numeri vivi: giorno della campagna (es. «giorno 6 di 30»),
   valore totale dei portafogli della campagna contro il traguardo dell'obiettivo, esercitazioni fatte su
   previste, giorni alla decisione «sì/no» del 23/12, strumenti di Neurograph in uso.
2. **La strada**, una linea del tempo verticale per tappe (`fatto` · `in corso` · `prossimo` · `futuro`), con
   un segno «sei qui». Ogni tappa ha: titolo, **cosa vuol dire** (2-4 frasi semplici), **perché conta**, date,
   chi la fa (SMTrading · Luca/Neurograph · entrambi · Matteo), e aprendola i **dettagli**: le voci di lavoro
   con il loro stato e il riferimento al progetto/piano.
3. **Neurograph**: i suoi esperimenti e strumenti come tappe proprie (fatti, in corso, prossimi), allineati
   alla pagina di Luca.
4. **Glossario** in fondo: agenti, Paperclip, MCP, broker, conto di prova/demo/reale, backtest, forchetta di
   rischio, scalping, intraday, campagna, go/no-go… spiegati in una riga.

Tappe di partenza (le scrive chi costruisce, con questi contenuti): fondamenta (piani 1-17: portafogli,
broker, agenti, rischio, portale); prove reali e sorveglianza; campagna di dicembre (23/11 → 23/12) con
esercitazioni e pagelle; lavoro in giornata e regole automatiche (costruito, prove 27/11 e 30/11, test agenti
3k); Neurograph (get_forecast, eventi, invio dati, prossimi strumenti); Fiume dei flussi; decisione «sì/no»
del 23/12; dopo: intraday con prezzi in tempo reale, passaggio al reale, opzioni (progetto separato).

## Come resta aggiornata

- File unico `backend/src/smtrading/roadmap/roadmap.yaml` (dentro il pacchetto, così entra nell'immagine).
- **Regola**: ogni lavoro futuro aggiorna anche la roadmap nello stesso commit (come il Fiume dei flussi).
- **Guardie**: un test fallisce se un file in `docs/superpowers/specs/` o `docs/superpowers/plans/` non è citato
  da nessuna voce; un test valida il file (campi obbligatori, stati ammessi, date coerenti).
- **Numeri vivi** calcolati dalla rotta: campagna e obiettivo da `goals`/`equity_snapshots` (portafogli in
  `total_portfolio_ids`), esercitazioni dalle voci della roadmap marcate come esercitazione (stato e data),
  strumenti di Neurograph dagli strumenti MCP che chiamano Neurograph (`flow.map.TOOL_DEST == "ng"`).

## Rotta e pagina

- `GET /api/roadmap` → `{ updated, summary, live: {...}, stages: [...], glossary: [...] }`, accesso normale
  del portale (admin e sola lettura).
- Pagina `/roadmap`, voce di menu «Roadmap» visibile a tutti gli utenti; disegno chiaro, leggibile, in tema
  con il portale; funziona su telefono.

## Lasciato a chi costruisce

Schema esatto del YAML, disposizione della linea del tempo, testi delle tappe (in italiano semplice, frasi
brevi, nessuna sigla non spiegata), test del web.
