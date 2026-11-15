# SMTrading — Gestione utenti del portale (design breve)

Data: 2026-11-15 · Stato: APPROVATO dall'utente il 2026-11-15 · Priorità: alta, sul ramo `main`, prima del piano 9.

## Perché
Oggi il portale ha un solo amministratore, creato all'installazione. L'utente vuole poter dare accesso ad altre persone: amministratori, oppure persone che devono solo seguire.

## Decisioni dell'utente
- **Ruoli:** `admin`, che può fare tutto come oggi, e `viewer` («Sola lettura»).
- **Primo accesso:** con una password temporanea mostrata una sola volta. Al primo login va cambiata obbligatoriamente.
- **TOTP:** facoltativo per tutti. Resta obbligatorio, come oggi, dove già lo è (azioni sul denaro reale).
- **Sola lettura con emergenza:** il viewer può usare **solo** due azioni protettive: «Disattiva denaro reale» (disarm) e l'**attivazione** del kill switch. Non può disattivare il kill switch.

## Design
- **Modello:** `admin_users` riceve `role` (`admin` o `viewer`, default `admin` per gli utenti esistenti), `must_change_password` (bool), `disabled_at` e `created_by`. Migrazione alla prima numerazione libera su `main`, cioè 0022. Il piano 9 ha già una 0022 sul suo ramo: chi integra i due rami deve rinumerare in modo lineare, per esempio i task del piano 9 passano da 0023 in poi (il controller lo fa al merge di main nel ramo del piano 9).
- **Autorizzazione:**
  - `require_admin` resta per le scritture; si aggiunge `require_user` per le letture.
  - Ogni route GET dell'admin accetta entrambi i ruoli. Ogni metodo non sicuro (POST, PUT, PATCH, DELETE) richiede `admin`.
  - Eccezioni consentite al viewer: logout, cambio della propria password, setup/enable/disable del proprio TOTP, `POST …/live-links/{id}/disarm`, attivazione del kill switch (solo verso `on`).
  - Un **test di copertura** elenca tutte le route dell'app e verifica che ogni route non sicura neghi il viewer, salvo l'elenco esplicito delle eccezioni. Una route nuova dimenticata deve far fallire il test.
- **Password temporanea:** generata dal server, almeno 16 caratteri, mostrata solo nella risposta di creazione o reset, mai registrata in chiaro né nei log. Con `must_change_password` attivo, ogni route tranne me, logout e cambio password risponde 403 con codice `PASSWORD_CHANGE_REQUIRED` e messaggio in italiano. Il portale mostra subito il modulo di cambio.
- **Pagina «Utenti»** in Impostazioni, solo per admin:
  - elenco con email, ruolo, TOTP sì/no, ultimo accesso, stato;
  - azioni: crea, cambia ruolo, disattiva/riattiva, reimposta password, azzera TOTP;
  - niente cancellazione fisica: si disattiva.
- **Protezioni:**
  - non si può modificare il proprio ruolo né disattivare se stessi;
  - deve restare almeno un admin attivo, verificato dentro la transazione con un lock;
  - disattivazione, cambio di ruolo, reimpostazione della password e azzeramento del TOTP **revocano subito tutte le sessioni** di quell'utente (`revoke_other_sessions` con keep vuoto);
  - creare un admin, promuovere a admin o reimpostare la password di un admin richiede il TOTP di chi agisce, **se** chi agisce ha il TOTP attivo;
  - email normalizzata e unica, con le regole già usate dal login.
- **Audit e avvisi:** ogni azione va nel registro admin. La creazione di un admin, una promozione ad admin e la riattivazione di un admin generano un messaggio Telegram «system» di sicurezza, senza password.
- **Portale:**
  - il viewer vede le stesse pagine, senza pulsanti di modifica (un flag `role` in `/api/auth/me`);
  - il banner rosso e il pulsante disarm restano visibili e attivi;
  - il pulsante del kill switch è attivo solo verso «attiva».
- **CLI:** `python -m smtrading.cli users list|create|set-role|disable|reset-password` per il recupero d'emergenza. Il comando create con `--role admin` serve se si perde l'accesso. Stesse protezioni, compreso «almeno un admin».

## Non-obiettivi
Inviti via email, ruoli più dettagliati, single sign-on, cancellazione fisica degli utenti.

## Task (eseguiti con TDD e revisione)
1. **Backend:** migrazione e modello, `require_user` e regole per ruolo con test di copertura delle route, password temporanea e `must_change_password`, route `/api/users` con protezioni, audit e Telegram, CLI.
2. **Web:** pagina Utenti, modulo di cambio password obbligatorio, viewer senza pulsanti di modifica tranne le emergenze, test Vitest.
