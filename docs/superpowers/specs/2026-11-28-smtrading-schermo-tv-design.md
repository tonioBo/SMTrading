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

## Vincoli

- Sola lettura; il link della TV non dà accesso ad altro.
- Il token della TV è un segreto: mai nei log, nelle risposte, su Telegram.
- Nessuna modifica al percorso degli agenti, al motore o al catalogo MCP.
- La sezione è per gli amministratori; la messa in linea con gli agenti fermi.
- Ogni pezzo nuovo compare nella Roadmap e, se è un flusso nuovo (SMTrading → TV), nel Fiume dei flussi.

## Lasciato a chi costruisce

Libreria o codice per il WebSocket Samsung (preferire codice proprio piccolo su `websockets`/`httpx` già usati),
identificativo dell'app browser di Tizen, durata e firma del link di visualizzazione, disposizione delle
schermate.
