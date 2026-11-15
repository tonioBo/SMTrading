# SMTrading — Registro attività utenti (design breve)

Data: 2026-11-15 · Stato: APPROVATO dall'utente il 2026-11-15 · Ramo: `main` (dopo la gestione utenti).

## Perché
Con più persone sul portale (admin e «Sola lettura») serve sapere chi ha fatto cosa, quando e da dove, anche quando l'azione è fallita o è stata negata.

## Decisioni dell'utente
- **Tutto ciò che cambia stato è registrato**, in automatico: ogni richiesta con metodo non sicuro (POST, PUT, PATCH, DELETE) su `/api/*` fatta da una persona, admin o viewer, comprese le azioni di emergenza del viewer e le richieste negate.
- **Cosa si registra:** chi (id, email, ruolo), quando, IP e user agent, metodo e **modello della route** (`/api/users/{user_id}/role`, mai il percorso grezzo), gli id dai parametri del percorso, l'esito (stato HTTP e codice d'errore) e un **riepilogo ripulito** della richiesta.
- **Mai segreti:** password, codici TOTP, chiavi API, token, credenziali dei broker, token del bot Telegram. Si oscurano per nome della chiave e con il filtro dei segreti dei log; il riepilogo ha un tetto di dimensione.
- **Eventi di accesso:** login riuscito, login fallito (email tentata, mai la password), logout, cambio password, cambio obbligatorio della password temporanea, setup/attivazione/disattivazione TOTP, chiusura delle altre sessioni, blocchi (troppi tentativi di login, blocco del TOTP di conferma).
- **Pagina «Registro attività»**, solo admin (il viewer riceve 403): tabella con ora, utente, ruolo, azione in italiano, obiettivo, esito, IP; filtri per utente, tipo di azione, esito e periodo; «Carica altri»; export CSV lato server (UTF-8 con BOM, `;`, celle-formula neutralizzate come nel registro denaro reale).
- **Append-only, per sempre:** tabella `user_activity` con trigger che blocca UPDATE, DELETE e TRUNCATE (come `live_audit`), migrazione 0023. Nessuna cancellazione.

## Design
- **Meccanismo generico:** un middleware ASGI avvolge tutta l'app. Per ogni richiesta non sicura su `/api/*` crea un contesto (id richiesta, IP, user agent, corpo letto fino a 64 KB) e, a risposta finita, conosce route, parametri e stato. Una route nuova non può sfuggire: non serve nessuna dipendenza da ricordare. `require_session_user` e il login annotano chi agisce.
- **Transazione:** la sessione DB della richiesta (`get_db`) scrive la riga **nella stessa transazione** al primo commit (evento `before_commit`, dentro un savepoint): se lo stato cambia, la riga c'è. Se la richiesta non fa commit (fallita, negata, solo Redis come logout), il middleware scrive la riga dopo la risposta in una transazione breve a parte. Se dopo il commit la richiesta fallisce comunque, si aggiunge una seconda riga d'errore con lo stesso `request_id`.
- **Un errore di scrittura del registro non rompe mai l'azione:** il savepoint fallito viene registrato nei log e si riprova dopo la risposta; se fallisce anche quello, log e **un solo** avviso Telegram «system» finché il registro non torna a funzionare.
- **Azioni:** ogni coppia (metodo, route) ha una chiave `categoria.verbo` e un'etichetta italiana (catalogo nel backend, usato da API, CSV e pagina). Le route di accesso cambiano azione con l'esito (`auth.login`, `auth.login_failed`, `auth.login_locked`). Il blocco del TOTP di conferma aggiunge l'evento `auth.totp_lockout`.
- **Righe `admin:*` esistenti:** restano in `mcp_audit_log` come audit di dominio (valori vecchi/nuovi) e ricevono `request_id` nei parametri, che le collega alla riga di `user_activity`. Il registro attività è l'unico record della richiesta della persona: nessun doppione nella pagina.
- **Test:** un test di copertura elenca tutte le route non sicure dell'app, le chiama e verifica che ognuna produca una riga con route e azione giuste; ogni route ha un'etichetta. Il test di copertura dei ruoli include le nuove letture solo admin.
