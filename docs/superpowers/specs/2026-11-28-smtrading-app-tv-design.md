# App «SMTrading TV» per Samsung (Tizen) — progetto

Data: 28/11/2026 · Richiesta di Matteo dopo la prima prova dello Schermo TV nel browser: «non è che sia un
granché, non si può fare un'app da installare nella tv?». Problema indicato: aprire il browser, scrivere
l'indirizzo, accettare il certificato.

## Cosa

Un'app per la smart TV Samsung (Tizen, modello UE85DU7190 del 2024) installata in modalità sviluppatore
(non dallo store), con icona nel menu delle app:
- all'apertura va direttamente alle schermate di SMTrading (stesse dello Schermo TV: Campagna e portafogli,
  Salute e avvisi, Fiume dei flussi per gli amministratori, rotazione) senza barre del browser;
- se non è abbinata mostra il **codice di 6 cifre** (stesso abbinamento già in linea: «Schermo TV → Abbina con
  codice»); poi si ricorda da sola;
- **telecomando**: frecce ←/→ cambiano schermata, OK mette in pausa/riprende la rotazione, tasti colorati come
  scorciatoie (rosso Salute, verde Campagna, giallo Fiume, blu rotazione), Indietro esce; la scelta fatta dal
  portale continua a valere;
- suoni come nello Schermo TV (stesse impostazioni dell'utente);
- «Manda alla TV» dal portale apre l'app invece del browser (se installata).

## Il nodo del certificato (decisione)

Il portale usa https con un certificato della rete di casa che la TV non conosce: dentro un'app Tizen le
chiamate https a un certificato sconosciuto vengono rifiutate e non si possono accettare a mano. Scelta di
Claude: l'app parla con SMTrading su un **ingresso dedicato in http solo per l'app TV**, sulla rete di casa:
- espone solo i dati delle schermate TV e l'abbinamento a codice (le stesse rotte `tv-display`), nient'altro;
- accetta richieste solo da indirizzi della rete di casa;
- l'app tiene il proprio link di visualizzazione (dopo l'abbinamento a codice) e lo manda in un'intestazione,
  mai nell'indirizzo; il link resta revocabile dal portale come oggi.
Rischio dichiarato: sulla rete di casa i dati delle schermate e il link viaggiano senza cifratura; sono dati in
sola lettura e il link si revoca dal portale. Alternativa se Matteo non vuole http: certificato pubblico con
un nome di dominio per la rete di casa (più lavoro, fuori da questo progetto).

## Installazione (una volta, con Matteo davanti alla TV)

1. Sulla TV: modalità sviluppatore attivata con l'indirizzo del PC di SMTrading.
2. Account Samsung gratuito per il certificato dell'app (autore + distributore Samsung).
3. Claude costruisce il pacchetto `.wgt`, lo firma e lo installa dalla rete; prima di toccare la TV chiede
   l'ok su Telegram (regola già in memoria).
4. Le app installate così scadono dopo un periodo deciso da Samsung: SMTrading avvisa prima della scadenza e
   la reinstallazione la fa Claude dalla rete.

## Vincoli

Sola lettura; nessun segreto nel pacchetto dell'app; codice in inglese, testi in italiano; il portale e
la Roadmap aggiornati; il Fiume dei flussi mostra il flusso SMTrading → TV come oggi; messa in linea della
parte server con gli agenti fermi.
