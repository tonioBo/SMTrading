# Schermo TV — progetto

Data: 28/11/2026 · Scelte di Matteo: strada B (invio automatico alla TV), schermate Fiume dei flussi, Campagna e
portafogli, Salute e avvisi.

## Perché

Vedere SMTrading sulla smart TV di casa senza toccare il PC: scegliere dal portale cosa mostrare e mandarlo alla
TV, con schermate leggibili da lontano.

## La TV trovata in rete (28/11)

Samsung 85" Crystal UHD, modello UE85DU7190UXZT (2024, Tizen), cablata, 3840×2160, API di rete Samsung sulla
porta 8001 (WebSocket sicuro sulla 8002) con autorizzazione a token; raggiungibile dal container `api`.
L'indirizzo **non** va scritto nel codice: si configura dal portale (campo nella sezione) o si trova con una
ricerca sulla rete locale avviata dal portale.

## Come funziona (strada B)

1. **Collegamento della TV** (una volta): dal portale «Collega la TV» → SMTrading apre la connessione di
   controllo remoto della TV (`wss://<tv>:8002/api/v2/channels/samsung.remote.control?name=<base64 "SMTrading">`)
   → la TV mostra «Consentire?» → Matteo accetta col telecomando → SMTrading salva il token della TV (segreto,
   cifrato come gli altri segreti del portale, mai mostrato).
2. **Manda alla TV**: Matteo sceglie una schermata (o la rotazione) e preme «Manda alla TV». Se la TV è in
   standby, SMTrading prova ad accenderla (tasto di accensione via la stessa connessione; se non riesce, lo dice).
   Poi apre il browser della TV (`ed.apps.launch` dell'app browser di Tizen) sull'indirizzo della schermata.
3. **Accesso della TV alle schermate**: un **link di visualizzazione** firmato, solo lettura, che apre solo le
   schermate TV (niente altro del portale), revocabile dal portale; la TV non conosce password.
4. **Telecomando dal portale**: cambiare schermata, avviare o fermare la rotazione (intervallo scelto, per
   esempio 30 s), «spegni lo schermo» (torna in standby). La pagina sulla TV segue la scelta fatta dal portale
   (controllo ogni pochi secondi), così non serve riaprire il browser a ogni cambio.
5. **Se l'apertura automatica non riesce** (modello o impostazioni): la sezione mostra l'indirizzo e un codice QR
   da aprire a mano sulla TV una volta (strada A come riserva).

## Le schermate TV (4K, leggibili da 3-4 metri)

- **Fiume dei flussi**: il fiume a tutto schermo, contatori e diario grandi, niente menu né comandi.
- **Campagna e portafogli**: giorno della campagna, valore totale e andamento verso l'obiettivo, per portafoglio
  valore e variazione del giorno, posizioni aperte con stop, ultimi ordini eseguiti.
- **Salute e avvisi**: stato di servizi, flussi prezzi, broker, Neurograph, agenti (dalla salute già esistente e
  da quella del Fiume v2), ultimi avvisi di sistema (solo titolo e ora, nessun testo sensibile).
- **Rotazione** tra le tre.

Caratteri grandi, contrasto alto, nessuna interazione richiesta, aggiornamento automatico, nessun dato segreto.

## Area riservata per utente (Matteo, 28/11: «prepara un'area riservata per permettere a chi ha accesso di fare la stessa cosa, per utente»)

- La sezione «Schermo TV» è disponibile a **ogni utente del portale** (amministratori e sola lettura), ognuno nella
  **propria area**: le sue TV (indirizzo, nome, token cifrato), i suoi link di visualizzazione, la sua schermata o
  rotazione corrente. Un utente non vede né usa le TV o i link di un altro.
- Gli amministratori vedono l'elenco di tutte le TV collegate (utente, nome, ultimo uso) e possono revocarle.
- Le azioni sulla propria TV sono permesse anche agli utenti di sola lettura (non toccano il trading) e sono
  registrate nel registro delle attività come le altre.
- Le schermate mostrano gli stessi dati che quell'utente vede nel portale.
- Limite di rete dichiarato nella pagina: i comandi partono dal server di SMTrading, quindi la TV deve essere
  raggiungibile da lì (stessa rete di casa o rete privata collegata); se non lo è, resta la strada A (aprire a
  mano sulla TV l'indirizzo con il link personale, mostrato anche come codice QR).

Decisioni di Matteo (28/11): la ricerca automatica delle TV sulla rete è **solo per gli amministratori**; la TV
in standby si accende **solo** quando l'utente preme «Manda alla TV» (rotazione e cambi di schermata non la
riaccendono mai).

## Vincoli

- Sola lettura; il link della TV non dà accesso ad altro.
- Il token della TV è un segreto: mai nei log, nelle risposte, su Telegram.
- Nessuna modifica al percorso degli agenti, al motore o al catalogo MCP.
- La sezione è per ogni utente, ciascuno nella sua area; la messa in linea con gli agenti fermi.
- Ogni pezzo nuovo compare nella Roadmap e, se è un flusso nuovo (SMTrading → TV), nel Fiume dei flussi.

## Lasciato a chi costruisce

Libreria o codice per il WebSocket Samsung (preferire codice proprio piccolo su `websockets`/`httpx` già usati),
identificativo dell'app browser di Tizen, durata e firma del link di visualizzazione, disposizione delle
schermate.
