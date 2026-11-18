# Passaggi tra agenti e verso gli umani

Mappa di **chi passa il lavoro a chi**, con quale task e quando, ricavata dalle istruzioni
(`00`–`09`), dalle Routine configurate in Paperclip e dai task che SMTrading apre da sé.
È un documento di controllo per chi gestisce il sistema: **non** va incollato negli agenti.
Le regole generali del passaggio (nuovo task, un solo assegnatario, «copia» = menzione,
chi chiude, cosa fare senza risposta) sono in `00-regole-comuni.md` §4 «Passaggi tra
agenti».

Legenda: **O** = obbligatorio (anche se non è successo nulla) · **C** = condizionale
(condizione nella colonna) · NA, PM, RM, ES, MA, QR, PA, NOT = agenti come nel README ·
`<data>` = `AAAA-MM-GG`. «Risposta» = commento nel task ricevuto; «chiude» = stato done.

## 1. Routine (cicli programmati, Europe/Rome)

| Ora | Routine (titolo in Paperclip) | Agente | Istruzioni | Uscita attesa |
|---|---|---|---|---|
| lun-ven 08:30 | Scansione pre-mercato 08:30 | NA | `06` «Scansione pre-mercato delle 08:30» | **O** task «AGENDA <data>» al PM; task della Routine chiuso con l'id |
| lun-ven 08:45 | — (SMTrading, nessun agente) | — | — | Telegram «Pronti per l'apertura» / problemi / «Mercati chiusi oggi» |
| lun-ven 09:00-22:00 ogni ora :00 | Sentinella rischio (oraria) | RM | `03` «Ciclo programmato (sentinella)» | **O** una riga per portafoglio; task solo se serve (sotto) |
| lun-ven 09:05-21:05 ogni ora :05 | Sentinella portafogli (oraria) | PM | `02` «Ciclo programmato (sentinella)» | «Nessuna novità» o proposte; alle 09:05 controllo BUONGIORNO |
| lun-ven 15:35 | Sentinella portafogli (oraria), 2° cron | PM | `02` sentinella, punto 6 (apertura USA) | giro opportunità; ANALISI se c'è uno slot libero |
| lun-ven 17:05-17:30, 21:05-22:00 | (i cicli PM che cadono nelle finestre) | PM | `02` sentinella, punto 7 | controllo barra in formazione; PROPOSTA se il segnale scatta |
| lun-ven 10-22 ogni 2 ore | Scansione notizie (ogni 2 ore) | NA | `06` «Scansione rapida» | «Nessuna notizia materiale» o task NOTIZIA |
| lun-ven 22:15 | Chiusura USA (serale) | PM | `02` «Ciclo Chiusura USA (serale)» | «Nessun segnale alla chiusura» o PROPOSTA per l'apertura successiva |
| sab-dom 10, 13, 16, 19, 22 | Sentinella rischio (weekend, crypto) | RM | `03` sentinella | come la sentinella feriale, solo crypto |
| sab-dom 10:05 … 22:05 ogni 3 ore | Sentinella portafogli (weekend, crypto) | PM | `02` sentinella | come la sentinella feriale, solo crypto |
| venerdì 16:00 | Richiesta audit settimanale | CEO | `01` «Ciclo programmato — richiesta audit» | **O** task «AUDIT SETTIMANALE · …» al PA |
| venerdì 17:00 | Revisione settimanale | CEO | `01` «Ciclo programmato — revisione settimanale» | **O** report + task «NOTIFICA · revisione settimanale · ALL» al NOT |

Ogni Routine apre un task all'agente: va **sempre chiuso** a fine ciclo, altrimenti i cicli
successivi vengono saltati (sovrapposizione disattivata). «Ronda ticket» è archiviata.

## 2. Cosa fa SMTrading da sé

| Quando | Cosa | Verso | Note per gli agenti |
|---|---|---|---|
| 07:00, 18:00, ven 18:30 | riepiloghi notte / giorno / settimana | Telegram | il NOT non li ripete |
| lun-ven 08:45 | controllo pre-apertura (con una riga per obiettivo) | Telegram | non sostituisce il BUONGIORNO del PM |
| evento | ordine eseguito, rifiutato, kill switch, perdita giornaliera, margine, halt attivato/revocato, broker | Telegram (`already_sent: true` nel feed) | il NOT non li ripete |
| ogni 5 min, posizione senza stop valido | Telegram «Posizione senza protezione» + task **«PROTEGGI · <nome portafoglio> · <simbolo>»** (priorità critica) | RM | uno per posizione; se il problema torna, nuovo task |
| ogni 5 min, obiettivo `min` passa «in ritardo» | Telegram + task **«OBIETTIVO <obiettivo> · in ritardo · <periodo>»** (alta) | CEO | uno per obiettivo e periodo |
| ogni 5 min, obiettivo `max_loss` «vicino al limite» / «superato» | Telegram + 2 task **«RISCHIO OBIETTIVO <obiettivo> · vicino al limite\|limite superato · <periodo> · Risk Manager\|CEO»** (alta / critica) | RM e CEO (uno ciascuno) | uno per obiettivo, periodo e stato |
| obiettivo `cap` «oltre il tetto», recuperi, raggiunto | solo Telegram | — | nessun task: lo segnala il PM con «OBIETTIVO <data>» |

## 3. Catena del mattino (lun-ven)

```text
08:30 Routine → NA: scansione pre-mercato
        └─ O nuovo task «AGENDA <data>» → PM (copia RM)      [06 punto 5]
08:45 SMTrading → Telegram: controllo pre-apertura           [automatico]
PM (all'assegnazione, al più tardi alle 09:05)
        ├─ risponde e chiude AGENDA                          [02 «AGENDA» 1]
        ├─ O nuovo task «BUONGIORNO <data>» → NOT            [02 «AGENDA» 3]
        └─ C nuovo task «OBIETTIVO <data>» → CEO             [02 «AGENDA» 4]
           (portafoglio senza strategia o obiettivo in ritardo senza ingressi; max 1/giorno)
NOT → Telegram «Buongiorno – piano di oggi <data>» (info), chiude il task [09]
09:05 PM senza AGENDA di oggi → BUONGIORNO comunque, «agenda notizie non pervenuta» [02]
```

## 4. Matrice dei passaggi

| # | Innesco | Da | A | Artefatto | O/C | Chi riceve fa | Chiude il giro |
|---|---|---|---|---|---|---|---|
| 1 | Routine 08:30 | NA | PM (copia RM) | task «AGENDA <data>» | **O** ogni lun-ven | valuta, poi #2 (e #3) | PM risponde e chiude AGENDA |
| 2 | AGENDA ricevuta (o 09:05 senza AGENDA) | PM | NOT | task «BUONGIORNO <data>» | **O** 1 al giorno | `get_goals`, `send_telegram` info | NOT risponde con `outbox_id` e chiude |
| 3 | AGENDA: portafoglio senza strategia / obiettivo in ritardo senza ingressi / tetto superato | PM | CEO | task «OBIETTIVO <data>» | C, max 1 al giorno | «Piano per gli obiettivi» | CEO risponde con il piano e chiude |
| 4 | SMTrading: obiettivo min in ritardo | SMTrading | CEO | task «OBIETTIVO <obiettivo> · in ritardo · <periodo>» | C | come #3 | CEO risponde e chiude |
| 5 | Piano obiettivi / revisione / utente | CEO | QR | task «RICERCA STRATEGIA · <COD> · <idea>» | C (in ritardo o senza strategia; max 2 per portafoglio) | backtest, #6 | QR risponde con gli id APPROVA STRATEGIA (o «nessuna candidata») e chiude |
| 6 | Ricerca conclusa con candidata | QR | CEO (copia PM) | task «APPROVA STRATEGIA <STR-id> · <COD> · <strategy>», uno per candidata; `log_decision` other | C | decide lo stesso giorno, `log_decision` strategy | CEO risponde e chiude |
| 7 | Strategia approvata | CEO | PM | task «STRATEGIA APPROVATA <STR-id> · <COD>» | C (solo se approvata) | alert, watchlist; #8 se manca l'allocazione | PM risponde e chiude |
| 8 | Allocazione mancante | PM | CEO | task «ALLOCAZIONE · <COD>»; `log_decision` allocation (proposta) | C | `log_decision` allocation | CEO risponde con l'esito e chiude |
| 9 | Sentinella 09:05/15:35, slot libero | PM | MA | task «ANALISI · <COD> · <simboli o tema>» | C | analisi (max 10 simboli) | MA risponde nel task e chiude; PM legge al ciclo dopo |
| 10 | Segnale valido (sentinella, finestre pre-chiusura, 22:15, ANALISI, NOTIZIA) | PM | RM | task «PROPOSTA <PROP-id> · <COD> · <SIMBOLO> · <buy\|sell>»; `log_decision` proposal | C | checklist, `log_decision` approval/veto, poi #11 o #12 | RM risponde e chiude PROPOSTA |
| 11 | Proposta approvata | RM | ES | task «ESEGUI <PROP-id> · <COD> · <SIMBOLO> · <lato>» | C | `preview_order`, `place_bracket_order` | ES risponde «ESITO …» e chiude (aperto se «in attesa di fill») |
| 12 | Veto | RM | PM | task «VETO <PROP-id>» | C | corregge (`-v2`, nuova PROPOSTA) o rinuncia; 2° veto → #22 | PM risponde e chiude |
| 13 | Chiusura discrezionale | PM | RM | task «CHIUSURA · <COD> · <SIMBOLO> · <qty>» | C | `log_decision` approval, #14 | RM risponde e chiude |
| 14 | Chiusura / protezione / intraday all'ultimo ciclo / margine | RM | ES | task «CHIUDI · <COD> · <SIMBOLO>» o «PROTEGGI · <COD> · <SIMBOLO>» | C | `close_position` / `place_oco_order` | ES risponde e chiude |
| 15 | SMTrading: posizione senza stop valido | SMTrading | RM | task «PROTEGGI · <nome portafoglio> · <simbolo>» | C | verifica, #14 | RM risponde con gli id degli ordini e chiude; ES senza esito al ciclo dopo → #22 critical |
| 16 | Movimento > 2 ATR e stop assente | PM | RM | task «POSIZIONE SENZA STOP · <COD> · <SIMBOLO>» | C | come #15 | RM risponde e chiude |
| 17 | Stop a favore | PM | ES (copia RM) | task «MODIFICA STOP · <COD> · <SIMBOLO>» | C | `modify_order` | ES risponde e chiude |
| 18 | Stop più largo | PM → RM → ES | RM, poi ES | task «MODIFICA STOP · …» al RM; se approva, nuovo «MODIFICA STOP · …» all'ES con `decision_id` | C | RM approva/veta; ES modifica | ognuno chiude il proprio |
| 19 | SMTrading: `max_loss` vicino / superato | SMTrading | RM e CEO | 2 task «RISCHIO OBIETTIVO … · Risk Manager\|CEO» | C | RM: veto aperture + #20; CEO: concorda, valuta halt | ognuno risponde e chiude il suo |
| 20 | RISCHIO OBIETTIVO (RM) | RM | PM (copia CEO) | task «RIDUZIONE RISCHIO · <COD\|ALL> · <obiettivo>» | C | niente aperture nell'ambito; #13 / #17 | PM risponde e chiude |
| 21 | Rischio complessivo in crescita | RM | CEO | task «HALT · <COD\|ALL> · <motivo>» | C | `set_trading_halt`, `log_decision` risk_rule, #21b | CEO risponde e chiude |
| 21b | Halt attivato o revocato | CEO | ES (copia PM, RM) | task «HALT · <COD\|ALL> · attivato\|revocato» | C | annulla ingressi in attesa se richiesto | ES risponde e chiude (Telegram lo manda SMTrading) |
| 22 | Condizioni §11 (2/3 perdita, disaccordo dopo 2 proposte, errori ripetuti, dati mancanti 2 cicli, serve un umano) | chiunque (RM, PM, ES, NA, MA, QR) | NOT (copia CEO; ES anche RM) | task «ESCALATION <ESC-id> · <COD\|ALL> · <tipo>» | C (vincolante) | `send_telegram` warning/critical (salvo `already_sent`) | NOT risponde e chiude; CEO commenta nello stesso task |
| 23 | Serve un umano (profilo, capitale, kill switch, sblocco, halt degli amministratori, strumento) | CEO | NOT | task «RICHIESTA UMANI · <tipo>» | C | `send_telegram` warning con domanda chiusa | NOT chiude; la risposta degli umani arriva al CEO in Paperclip |
| 24 | Decisione da comunicare | CEO (o chi decide) | NOT | task «NOTIFICA · <tipo> · <COD\|ALL>» | C (max 1 per decisione) | `send_telegram` info | NOT chiude |
| 25 | Notizia materiale (scansioni) | NA | PM (copia RM se negativa su posizione) | task «NOTIZIA · <SIMBOLO> · <impatto> · <urgenza>» | C (max 1 per simbolo per ciclo) | valuta proposta o chiusura (#10/#13) | PM risponde e chiude |
| 26 | Notizia eccezionale | NA | RM + NOT (copia CEO) | NOTIZIA urgente + «ESCALATION …» warning | C | vedi #22 | come #22 |
| 27 | Indicatori senza barra in formazione | PM | CEO | task «SEGNALAZIONE STRUMENTO · <SIMBOLO>» | C (1 per simbolo al giorno) | se serve un umano #23 | CEO risponde e chiude |
| 28 | Routine venerdì 16:00 | CEO | PA | task «AUDIT SETTIMANALE · <lun>–<ven>» | **O** ogni venerdì | audit 1w (anche senza operazioni) | PA risponde «AUDIT SETTIMANALE …» entro 17:00 e chiude |
| 29 | Non conformità alta / drawdown incoerente | PA | CEO | task «NON CONFORMITÀ · <COD> · <NC-id>» | C, stesso giorno | verifica, decide, `log_decision` | CEO risponde e chiude |
| 30 | Scostamento reale vs backtest | PA | QR | task «RIVEDI STRATEGIA» | C | **non descritto** (vedi «Da decidere») | — |
| 31 | Routine venerdì 17:00 | CEO | NOT | report nel task della Routine + task «NOTIFICA · revisione settimanale · ALL» | **O** ogni venerdì | `send_telegram` info | NOT chiude; CEO chiude la Routine |
| 32 | Proposta 22:15 | PM | RM | «PROPOSTA …» con validità all'apertura successiva | C | al primo ciclo RM (09:00, nel weekend crypto 10:00) | come #10 |

## 5. Da decidere

Contraddizioni o vuoti trovati tra i file, lasciati come sono in attesa di una scelta:

1. **«copia a X» in Paperclip.** Ora è definita come menzione di X in un commento del task
   (`00` §4). Va verificato che in Paperclip la menzione svegli l'agente menzionato; se no,
   per le copie su cui qualcuno deve agire (CEO sulle ESCALATION, RM sull'AGENDA) serve un
   task separato.
2. **RISCHIO OBIETTIVO: chi chiede al PM di fermarsi.** `01` dice che il CEO «chiede al
   Portfolio Manager di non aprire», `03` che il RM crea il task al PM (copia CEO). Il titolo
   «RIDUZIONE RISCHIO · …» e il suo trattamento in `02` sono nuovi: confermare che lo crea
   solo il RM.
3. **Annullare un ingresso non eseguito** (`02` serale punto 4, «segnale sparito alla
   chiusura»): non c'è un titolo di task né un passo dell'ES per `cancel_order` su richiesta
   del PM/RM («CHIUDI» fa `close_position`).
4. **Ingressi «in attesa di fill»:** l'ES non ha Routine e verifica i task rimasti aperti
   solo quando ne riceve uno nuovo; un ESEGUI può restare aperto a lungo. Serve un controllo
   (per esempio del RM in sentinella) o un ciclo dell'ES?
5. **«RIVEDI STRATEGIA»** (dal PA al QR, `08`): il QR non ha un passo per questo task
   (`07` cita solo «revisione strategie scadute» dal CEO). Va trattato come RICERCA
   STRATEGIA? Con esito al CEO?
6. **AUDIT GIORNALIERO:** il flusso in `00` §4 mostra un «report giornaliero» del PA al CEO,
   ma nessuna Routine e nessun agente lo chiede: oggi non parte mai da solo.
7. **NOTIZIA eccezionale** (`06` «Escalation»): «task NOTIZIA con urgenza alta al RM» mentre
   la NOTIZIA normale è assegnata al PM con copia al RM. Assegnatario da confermare.
8. **Giorni di mercati chiusi** (festività con borse UE/USA chiuse, crypto aperte): l'AGENDA
   e il BUONGIORNO restano obbligatori? Il controllo delle 08:45 manda già «Mercati chiusi
   oggi».
9. **Due nomi per lo stesso allarme:** «POSIZIONE SENZA STOP · …» (PM → RM) e «PROTEGGI ·
   …» (SMTrading → RM, RM → ES). Ora il RM tratta il primo come il secondo; si può tenere
   un nome solo.
10. **PROTEGGI con due forme di titolo:** SMTrading usa il nome intero del portafoglio
    («PROTEGGI · Portafoglio Aggressivo · NVDA»), il RM verso l'ES il codice («PROTEGGI ·
    AGG · NVDA»). Funziona, ma chi cerca per titolo deve saperlo.
11. **`LIVE_NOT_ARMED`** (`00` §12, `04`): «escalation al CEO e al Risk Manager» non dice se
    è un task ESCALATION al Notifier (§11) o un task diretto al CEO.
12. **Weekend:** prima `00` §6 diceva «nel weekend nessuno gira», ma le Routine crypto del
    weekend di PM e RM sono attive; il testo è stato allineato alle Routine. Confermare che
    restano attive.
