# SMTrading

Simulatore di trading per agenti AI: backend FastAPI (API admin, MCP, worker), Postgres,
Redis e l'app web di amministrazione in italiano, tutto in `docker compose`.

## Avvio

1. Copia `.env.example` in `.env` e compila i valori. `SMT_ADMIN_PASSWORD` deve avere
   **almeno 10 caratteri**: con una password più corta l'admin non viene creato.
2. `docker compose up -d --build`
3. Dal PC stesso: <http://127.0.0.1:3000/> (ingresso locale, http).

L'API (`127.0.0.1:8000`, anche per l'MCP) resta raggiungibile solo dal PC.

## Accesso dalla rete locale

Dagli altri dispositivi della rete l'app è servita **solo in https**:
<https://192.168.30.7/> (l'indirizzo http `http://192.168.30.7/` reindirizza a https).

- **Indirizzi ammessi.** L'app risponde solo agli indirizzi elencati in
  `SMT_PUBLIC_HOSTS` nel file `.env` (IP o nomi separati da virgola, predefinito
  `192.168.30.7`), oltre a `localhost`/`127.0.0.1`. Se l'IP del PC cambia, aggiorna
  `SMT_PUBLIC_HOSTS` e riavvia con `docker compose up -d web`: il certificato viene
  rigenerato con lo stesso certificato di autorità, quindi non va reinstallato.
- **Certificato.** Al primo avvio il container `web` crea una piccola autorità di
  certificazione locale ("SMTrading CA locale") e il certificato del server; sono salvati
  nel volume `web_certs`. Finché il certificato dell'autorità non è installato il browser
  mostra un avviso di sicurezza. Per installarlo scarica
  <http://192.168.30.7/smtrading-ca.crt> dal dispositivo e:
  - **Windows:** doppio clic sul file → *Installa certificato* → *Computer locale* →
    *Colloca tutti i certificati nel seguente archivio* → **Autorità di certificazione
    radice attendibili** → Fine. Riavvia il browser (Firefox: *Impostazioni → Privacy e
    sicurezza → Certificati → Mostra certificati → Autorità → Importa*).
  - **Android:** *Impostazioni → Sicurezza → Crittografia e credenziali → Installa un
    certificato → Certificato CA* e scegli il file scaricato (il percorso esatto varia
    con il produttore).
  - **iOS / iPadOS:** apri il file con Safari e consenti il download del profilo, poi
    *Impostazioni → Profilo scaricato → Installa*; infine *Impostazioni → Generali →
    Info → Impostazioni certificati attendibili* e attiva **SMTrading CA locale**.
  
  Installa solo il certificato scaricato dal tuo PC: chi possiede la chiave di questa
  autorità (nel volume `web_certs`) può emettere certificati considerati validi dai
  dispositivi che l'hanno installata.
- **Firewall di Windows.** Consenti in ingresso le porte TCP **80** e **443** solo per le
  reti private, ad esempio da PowerShell come amministratore:

  ```powershell
  New-NetFirewallRule -DisplayName "SMTrading web" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow -Profile Private
  ```

  Con Docker in WSL2 potrebbe servire anche l'inoltro delle porte dall'host a WSL
  (Docker Desktop lo fa da sé).
- **Accesso.** Dalla rete locale i tentativi di login sono limitati per indirizzo
  (nginx) e i tentativi falliti sono contati separatamente da quelli dell'ingresso locale,
  così un dispositivo in rete non può bloccare l'accesso dal PC. Le sessioni durano al
  massimo 24 ore (12 ore senza attività); da *Impostazioni* puoi chiudere tutte le altre
  sessioni e attivare l'autenticazione a due fattori.

## Test

- Backend: `cd backend && uv run pytest -q && uv run ruff check .`
- Web: `cd web && npm test -- --run && npm run lint && npm run build`
- End-to-end: `bash scripts/e2e.sh` avvia uno stack separato (progetto
  `smtrading-e2e`, con volumi, rete e credenziali proprie, web su `127.0.0.1:3190`), esegue
  Playwright e lo elimina alla fine; lo stack principale non viene toccato.
