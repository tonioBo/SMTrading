# Passaggi tra agenti e verso gli umani

Mappa di **chi passa il lavoro a chi**, con quale task e quando, ricavata dalle istruzioni
(`00`–`09`), dalle Routine configurate in Paperclip e dai task che SMTrading apre da sé.
È un documento di controllo per chi gestisce il sistema: **non** va incollato negli agenti.
Le regole generali del passaggio (nuovo task, un solo assegnatario, «copia» = menzione solo
informativa e task separato a chi deve agire, chi chiude, cosa fare senza risposta) sono in `00-regole-comuni.md` §4 «Passaggi tra
agenti».

Legenda: **O** = obbligatorio (anche se non è successo nulla) · **C** = condizionale
(condizione nella colonna) · NA, PM, RM, ES, MA, QR, PA, NOT = agenti come nel README ·
`<data>` = `AAAA-MM-GG`. «Risposta» = commento nel task ricevuto; «chiude» = stato done.

## 1. Routine (cicli programmati, Europe/Rome)

| Ora | Routine (titolo in Paperclip) | Agente | Istruzioni | Uscita attesa |
|---|---|---|---|---|
| lun-ven 08:30 | Scansione pre-mercato 08:30 | NA | `06` «Scansione pre-mercato delle 08:30» | **O** task «AGENDA <data>» al PM (non nei giorni di borse chiuse); task della Routine chiuso con l'id |
| lun-ven 08:45 | — (SMTrading, nessun agente) | — | — | Telegram «Pronti per l'apertura» / problemi / «Mercati chiusi oggi» |
| lun-ven 09:00-22:00 ogni ora :00 | Sentinella rischio (oraria) | RM | `03` «Ciclo programmato (sentinella)» | **O** controllo completo a ogni ciclo (niente uscita rapida); una riga per portafoglio; task solo se serve (sotto) |
| lun-ven 09:05-21:05 ogni ora :05 | Sentinella portafogli (oraria) | PM | `02` «Ciclo programmato (sentinella)» | **O** uscita rapida «Nessuna novità» se feed e alert vuoti (non alle 09:05/15:35, non in pre-chiusura, non con ingressi in attesa); altrimenti controllo ingressi in attesa (ANNULLA se scaduti), proposte; alle 09:05 controllo BUONGIORNO |
| lun-ven 15:35 | Sentinella portafogli (oraria), 2° cron | PM | `02` sentinella, punto 6 (apertura USA) | giro opportunità; ANALISI se c'è uno slot libero |
| lun-ven 17:05-17:30, 21:05-22:00 | (i cicli PM che cadono nelle finestre) | PM | `02` sentinella, punto 7 | controllo barra in formazione; PROPOSTA se il segnale scatta |
| lun-ven 10-22 ogni 2 ore | Scansione notizie (ogni 2 ore) | NA | `06` «Scansione rapida» | «Nessuna notizia materiale» o task NOTIZIA |
| lun-ven 22:15 | Chiusura USA (serale) | PM | `02` «Ciclo Chiusura USA (serale)» | «Nessun segnale alla chiusura» o PROPOSTA per l'apertura successiva |
| sab-dom 10, 13, 16, 19, 22 | Sentinella rischio (weekend, crypto) | RM | `03` sentinella | come la sentinella feriale, solo crypto |
| sab-dom 10:05 … 22:05 ogni 3 ore | Sentinella portafogli (weekend, crypto) | PM | `02` sentinella | come la sentinella feriale, solo crypto; niente AGENDA/BUONGIORNO |
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
| ogni 5 min, passaggio mancante | Telegram «⚠ Passaggio mancante: …» (uno per passaggio al giorno) | umani | nei giorni di borsa: AGENDA al PM entro le 08:50, BUONGIORNO al NOT e il suo Telegram entro le 09:20; sempre: PROTEGGI / RISCHIO OBIETTIVO senza risposta dopo 45 min, ESEGUI aperto dopo 30 min a mercato aperto. Nessun task: rispondi e chiudi i tuoi task in tempo |
| dopo un riavvio | allarmi (protezione, obiettivi, pre-apertura, passaggi) | — | il primo controllo parte almeno 3 min dopo l'avvio |

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
Borse dei portafogli tutte chiuse (festività) o weekend → niente AGENDA, niente BUONGIORNO
```

## 4. Matrice dei passaggi

| # | Innesco | Da | A | Artefatto | O/C | Chi riceve fa | Chiude il giro |
|---|---|---|---|---|---|---|---|
| 1 | Routine 08:30 | NA | PM (copia RM, informativa) | task «AGENDA <data>» | **O** ogni lun-ven con borse aperte (non nei giorni di borse dei portafogli tutte chiuse) | valuta, poi #2 (e #3) | PM risponde e chiude AGENDA |
| 2 | AGENDA ricevuta (o 09:05 senza AGENDA, borse aperte) | PM | NOT | task «BUONGIORNO <data>» | **O** 1 al giorno; mai con borse chiuse o nel weekend | `get_goals`, `send_telegram` info | NOT risponde con `outbox_id` e chiude |
| 3 | AGENDA: portafoglio senza strategia / obiettivo in ritardo senza ingressi / tetto superato | PM | CEO | task «OBIETTIVO <data>» | C, max 1 al giorno | «Piano per gli obiettivi» | CEO risponde con il piano e chiude |
| 4 | SMTrading: obiettivo min in ritardo | SMTrading | CEO | task «OBIETTIVO <obiettivo> · in ritardo · <periodo>» | C | come #3 | CEO risponde e chiude |
| 5 | Piano obiettivi / revisione / utente | CEO | QR | task «RICERCA STRATEGIA · <COD> · <idea>» | C (in ritardo o senza strategia; max 2 per portafoglio) | backtest, #6 | QR risponde con gli id APPROVA STRATEGIA (o «nessuna candidata») e chiude |
| 6 | Ricerca conclusa con candidata | QR | CEO (copia PM) | task «APPROVA STRATEGIA <STR-id> · <COD> · <strategy>», uno per candidata; `log_decision` other | C | decide lo stesso giorno, `log_decision` strategy | CEO risponde e chiude |
| 7 | Strategia approvata | CEO | PM | task «STRATEGIA APPROVATA <STR-id> · <COD>» | C (solo se approvata) | alert, watchlist; #8 se manca l'allocazione | PM risponde e chiude |
| 8 | Allocazione mancante | PM | CEO | task «ALLOCAZIONE · <COD>»; `log_decision` allocation (proposta) | C | `log_decision` allocation | CEO risponde con l'esito e chiude |
| 9 | Sentinella 09:05/15:35, slot libero | PM | MA | task «ANALISI · <COD> · <simboli o tema>» | C | analisi (max 10 simboli) | MA risponde nel task e chiude; PM legge al ciclo dopo |
| 10 | Segnale valido (sentinella, finestre pre-chiusura, 22:15, ANALISI, NOTIZIA) | PM | RM | task «PROPOSTA <PROP-id> · <COD> · <SIMBOLO> · <buy\|sell>»; `log_decision` proposal | C | checklist, `log_decision` approval/veto, poi #11 o #12 | RM risponde e chiude PROPOSTA |
| 11 | Proposta approvata | RM | ES | task «ESEGUI <PROP-id> · <COD> · <SIMBOLO> · <lato>» | C | `preview_order`, `place_bracket_order` | ES risponde «ESITO …» e chiude sempre, anche «in attesa (order_id …)» |
| 12 | Veto | RM | PM | task «VETO <PROP-id>» | C | corregge (`-v2`, nuova PROPOSTA) o rinuncia; 2° veto → #22 | PM risponde e chiude |
| 13 | Chiusura discrezionale | PM | RM | task «CHIUSURA · <COD> · <SIMBOLO> · <qty>» | C | `log_decision` approval, #14 | RM risponde e chiude |
| 14 | Chiusura / protezione / intraday all'ultimo ciclo / margine | RM | ES | task «CHIUDI · <COD> · <SIMBOLO>» o «PROTEGGI · <nome portafoglio> · <simbolo>» | C | `close_position` / `place_oco_order` | ES risponde e chiude |
| 15 | SMTrading: posizione senza stop valido | SMTrading | RM | task «PROTEGGI · <nome portafoglio> · <simbolo>» | C | verifica, #14 con lo stesso titolo | RM risponde con gli id degli ordini e chiude; ES senza esito al ciclo dopo → #22 critical |
| 16 | Movimento > 2 ATR e stop assente | PM | RM | task «PROTEGGI · <nome portafoglio> · <simbolo>» | C | come #15 | RM risponde e chiude |
| 17 | Stop a favore | PM | ES (copia RM) | task «MODIFICA STOP · <COD> · <SIMBOLO>» | C | `modify_order` | ES risponde e chiude |
| 18 | Stop più largo | PM → RM → ES | RM, poi ES | task «MODIFICA STOP · …» al RM; se approva, nuovo «MODIFICA STOP · …» all'ES con `decision_id` | C | RM approva/veta; ES modifica | ognuno chiude il proprio |
| 19 | SMTrading: `max_loss` vicino / superato | SMTrading | RM e CEO | 2 task «RISCHIO OBIETTIVO … · Risk Manager\|CEO» | C | RM **decide**: veto aperture + #20; CEO rivede, valuta halt, non scrive al PM | ognuno risponde e chiude il suo |
| 20 | RISCHIO OBIETTIVO (RM) | RM | PM (copia CEO, informativa) | task «RIDUZIONE RISCHIO · <COD> · <obiettivo>», uno per portafoglio | C | niente aperture nell'ambito; #13 / #17 | PM risponde e chiude |
| 21 | Rischio complessivo in crescita | RM | CEO | task «HALT · <COD\|ALL> · <motivo>» | C | `set_trading_halt`, `log_decision` risk_rule, #21b | CEO risponde e chiude |
| 21b | Halt attivato o revocato | CEO | ES (copia PM, RM) | task «HALT · <COD\|ALL> · attivato\|revocato» | C | annulla ingressi in attesa se richiesto | ES risponde e chiude (Telegram lo manda SMTrading) |
| 22 | Condizioni §11 (2/3 perdita, disaccordo dopo 2 proposte, errori ripetuti, dati mancanti 2 cicli, `LIVE_NOT_ARMED`, serve un umano) | chiunque (RM, PM, ES, NA, MA, QR) | NOT (copia CEO, informativa; ES e `LIVE_NOT_ARMED` anche RM) | task «ESCALATION <ESC-id> · <COD\|ALL> · <tipo>» | C (vincolante) | `send_telegram` warning/critical (salvo `already_sent`) | NOT risponde e chiude; il CEO agisce solo se serve (halt, #23) |
| 23 | Serve un umano (profilo, capitale, kill switch, sblocco, halt degli amministratori, strumento) | CEO | NOT | task «RICHIESTA UMANI · <tipo>» | C | `send_telegram` warning con domanda chiusa | NOT chiude; la risposta degli umani arriva al CEO in Paperclip |
| 24 | Decisione da comunicare | CEO (o chi decide) | NOT | task «NOTIFICA · <tipo> · <COD\|ALL>» | C (max 1 per decisione) | `send_telegram` info | NOT chiude |
| 25 | Notizia materiale (scansioni) | NA | PM (copia RM se negativa su posizione) | task «NOTIZIA · <SIMBOLO> · <impatto> · <urgenza>» | C (max 1 per simbolo per ciclo) | valuta proposta o chiusura (#10/#13) | PM risponde e chiude |
| 26 | Notizia eccezionale | NA | RM se tocca posizioni aperte, altrimenti PM; + NOT (copia CEO) | NOTIZIA urgente + «ESCALATION …» warning | C | vedi #22 | come #22; chi riceve la NOTIZIA risponde e chiude |
| 27 | Indicatori senza barra in formazione | PM | CEO | task «SEGNALAZIONE STRUMENTO · <SIMBOLO>» | C (1 per simbolo al giorno) | se serve un umano #23 | CEO risponde e chiude |
| 28 | Routine venerdì 16:00 | CEO | PA | task «AUDIT SETTIMANALE · <lun>–<ven>» | **O** ogni venerdì | audit 1w (anche senza operazioni) | PA risponde «AUDIT SETTIMANALE …» entro 17:00 e chiude |
| 29 | Non conformità alta / drawdown incoerente | PA | CEO | task «NON CONFORMITÀ · <COD> · <NC-id>» | C, stesso giorno | verifica, decide, `log_decision` | CEO risponde e chiude |
| 30 | Scostamento reale vs backtest | PA | QR | task «RIVEDI STRATEGIA <STR-id> · <COD>» | C | backtest sui dati recenti; se cambia: nuovo «APPROVA STRATEGIA …» al CEO (#6) | QR risponde «confermata» o «da rivedere/sospendere» (menziona il CEO) e chiude |
| 31 | Routine venerdì 17:00 | CEO | NOT | report nel task della Routine + task «NOTIFICA · revisione settimanale · ALL» | **O** ogni venerdì | `send_telegram` info | NOT chiude; CEO chiude la Routine |
| 32 | Proposta 22:15 | PM | RM | «PROPOSTA …» con validità all'apertura successiva | C | al primo ciclo RM (09:00, nel weekend crypto 10:00) | come #10 |
| 33 | Ingresso non eseguito che non vale più (segnale sparito alle 22:15, validità scaduta in sentinella) | PM | ES (copia RM, informativa) | task «ANNULLA <PROP-id>»; `log_decision` other | C | `cancel_order` su ingresso e uscite bracket; se già eseguito non tocca stop/target | ES risponde con gli id annullati e chiude |
## 5. Decisioni (18/11)

Le 12 questioni aperte trovate nell'audit, decise dal titolare il 18/11 e applicate nei file:

1. **«copia a X»** = menzione **solo informativa**; quando X deve agire, task separato
   assegnato a X (`00` §4). Il CEO in copia alle ESCALATION non risponde: agisce solo se serve.
2. **Rischio obiettivo:** decide il **Risk Manager**, che crea «RIDUZIONE RISCHIO · <COD> ·
   <obiettivo>» al PM; il CEO è menzionato e rivede, non scrive al PM (`01`, `02`, `03`).
3. **Ingresso non eseguito con segnale sparito:** il PM al ciclo delle 22:15 crea «ANNULLA
   <PROP-id>» all'ES; l'ES annulla l'ingresso e le sue uscite bracket, risponde con gli id e
   chiude (`02`, `04`).
4. **ESEGUI in attesa:** l'ES risponde «in attesa (order_id …)» e chiude; il PM ricontrolla gli
   ingressi in attesa a ogni sentinella e, se non valgono più, usa «ANNULLA» (`02`, `04`).
5. **RIVEDI STRATEGIA** (PA → Quant): il Quant rifà il backtest sui dati recenti; se cambia,
   nuovo «APPROVA STRATEGIA» al CEO, altrimenti risposta «confermata», poi chiude (`07`, `08`).
6. **AUDIT GIORNALIERO** tolto ovunque: resta solo l'audit settimanale del venerdì (`00`,
   `01`, `08`, README).
7. **NOTIZIA eccezionale:** al Risk Manager se tocca posizioni aperte, altrimenti al PM (`06`).
8. **Borse dei portafogli tutte chiuse** (festività) o weekend: niente AGENDA e niente
   BUONGIORNO; le crypto, sempre aperte, non contano (`02`, `06`).
9. e 10. **Un solo nome** per l'allarme di protezione: «PROTEGGI · <nome portafoglio> ·
   <simbolo>», anche PM → RM e RM → ES; «POSIZIONE SENZA STOP» non esiste più (`02`, `03`,
   `04`).
11. **`LIVE_NOT_ARMED`:** task ESCALATION al Notifier con CEO e Risk Manager menzionati
    (`00` §12, `04`).
12. **Weekend:** le sentinelle crypto di PM e RM restano (STR-106); testi allineati (`00`
    §6, `02`, `03`, README).

Nota tecnica sulla 9-10: dopo un timeout SMTrading, prima di ritentare, adotta un task
**aperto** con lo stesso titolo; con un titolo unico potrebbe adottare il «PROTEGGI» del PM o
quello dal RM all'ES invece di crearne uno al RM. Il caso è raro (solo dopo un timeout) e
l'allarme Telegram parte comunque.
