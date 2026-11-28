# App «SMTrading TV»: installazione sulla TV Samsung

L'app mostra le schermate dello Schermo TV (Campagna e portafogli, Salute e avvisi, Fiume dei
flussi per gli amministratori, rotazione) a tutto schermo, senza browser e senza certificati da
accettare, e si guida con il telecomando. Si installa in **modalità sviluppatore** (non dallo
store). Progetto: `docs/superpowers/specs/2026-11-28-smtrading-app-tv-design.md`.

**Regola:** l'installazione si fa solo con il proprietario davanti alla TV e dopo il suo ok su
Telegram. Nessuna password o certificato va scritto in questo documento, nei messaggi o nel
codice.

## Cosa serve (una volta sola)

1. **SMTrading in linea con la porta 8087** (ingresso http dell'app TV). Si mette in linea con
   gli agenti fermi, come ogni aggiornamento. Prova dal PC:
   `curl http://192.168.30.7:8087/tv-app/health` deve rispondere `{"ok":true,...}`.
   Se dalla TV non arriva nulla, il firewall di Windows deve lasciar entrare la porta 8087
   dalla rete privata (Docker Desktop di solito aggiunge la regola da solo).
2. **Tizen Studio** sul PC di SMTrading (va bene anche la sola versione «CLI», in WSL o in
   Windows) da <https://developer.tizen.org/development/tizen-studio/download>. Nel *Package
   Manager*, scheda *Extension SDK*, installare **Samsung TV Extensions** e **Samsung
   Certificate Extension**. Lo script cerca gli strumenti in `~/tizen-studio`; un'altra
   cartella si indica con `TIZEN_STUDIO=/percorso`.
3. **Un account Samsung** (gratuito), per il certificato con cui si firma l'app.

Per controllare cosa c'è e cosa manca: `scripts/tv-app.sh check` (non tocca la TV).

## 1. Modalità sviluppatore sulla TV

1. Accendere la TV e aprire **App** (dal menu principale).
2. Con il telecomando digitare **1 2 3 4 5** (sul telecomando senza tasti numerici: il tasto
   «123» apre il tastierino a schermo). Compare la finestra *Developer mode*.
3. Mettere **Developer mode** su **ON** e scrivere in *Host PC IP* l'indirizzo del PC di
   SMTrading sulla rete di casa (oggi **192.168.30.7**). Confermare con OK.
4. **Spegnere e riaccendere la TV** (spegnimento completo: tenere premuto il tasto di
   accensione finché compare il logo, oppure staccare la spina per 30 secondi). In alto in
   *App* compare la scritta *Developer mode*.

Da WSL le connessioni verso la TV escono con l'indirizzo di Windows, quindi l'indirizzo da
scrivere è sempre quello del PC sulla rete di casa.

## 2. Certificato Samsung (profilo di firma)

La TV accetta solo app firmate con un certificato Samsung che contiene il suo codice (DUID).

1. Collegare la TV al PC: `sdb connect <ip-della-tv>` (la TV deve essere in modalità
   sviluppatore). Il DUID si legge in Tizen Studio → *Device Manager*, oppure con
   `sdb -s <ip-della-tv>:26101 shell 0 getduid`.
2. Aprire Tizen Studio → *Tools* → **Certificate Manager** → «+».
3. Tipo **Samsung**, dispositivo **TV**. Nome del profilo: **smtrading-tv** (un altro nome si
   passa allo script con `TIZEN_PROFILE=...`).
4. *Author certificate*: crearne uno nuovo (nome a scelta, password a scelta) e accedere con
   l'account Samsung quando lo chiede.
5. *Distributor certificate*: privilegio **Public**, aggiungere il **DUID** della TV (se la TV è
   collegata compare da solo), accedere con lo stesso account Samsung, terminare.
6. Conservare la password del certificato in un posto sicuro (non nel progetto). I file del
   certificato restano nella cartella `SamsungCertificate/smtrading-tv` della home: tenerne una
   copia nei backup personali, perché per aggiornare l'app senza perdere l'abbinamento serve lo
   **stesso** certificato autore.

## 3. Installazione

Con il proprietario presente e dopo l'ok su Telegram:

```
scripts/tv-app.sh install 192.168.30.84
```

Lo script costruisce l'app (dalle stesse schermate del portale), la firma con il profilo,
si collega alla TV con `sdb` e la installa. Ogni passo che manca viene detto in chiaro
(strumenti, profilo, TV non raggiungibile). Altri comandi: `build` (solo costruzione),
`package` (costruzione e firma), `run <ip>` (apre l'app già installata).

L'app compare in **App** sulla TV con il nome «SMTrading TV».

## 4. Primo avvio

1. L'app chiede l'**indirizzo di SMTrading**: è già scritto (`http://192.168.30.7:8087`), basta
   premere OK. L'app controlla che risponda.
2. Compare un **codice di 6 cifre**. Nel portale: **Schermo TV → Abbina con codice**, scrivere il
   codice. In pochi secondi la TV mostra le schermate di chi ha scritto il codice.
3. L'app si ricorda indirizzo e collegamento: alle aperture successive va subito alle schermate.

## Telecomando

| Tasto | Cosa fa |
|---|---|
| ◀ ▶ | schermata precedente / successiva |
| OK (o Play/Pausa) | mette in pausa o riprende la rotazione |
| Rosso | Salute e avvisi |
| Verde | Campagna e portafogli |
| Giallo | Fiume dei flussi (solo amministratori) |
| Blu | accende o spegne la rotazione; sulla schermata del codice: cambia l'indirizzo del server |
| Indietro | chiude l'app |

Quello che si sceglie dal portale (schermata, rotazione, intervallo, suoni) continua a valere:
quando cambia nel portale, la TV lo segue. I suoni sono quelli dello Schermo TV, con le stesse
impostazioni; se la TV tiene l'audio bloccato, premere OK una volta.

**«Manda alla TV»** dal portale accende la TV se è in standby e apre l'app (se è installata);
altrimenti apre il browser come prima.

## Revoca e nuovo abbinamento

- Dal portale (**Schermo TV → revoca il link**, o un link nuovo) la TV smette subito di
  ricevere i dati e torna a mostrare il codice: si abbina di nuovo come al primo avvio.
- Per cambiare l'indirizzo del server: sulla schermata del codice, tasto **blu**.

## Scadenza e rinnovo

- Le app installate in modalità sviluppatore valgono finché il certificato distributore
  Samsung è valido; la modalità sviluppatore può spegnersi dopo un aggiornamento della TV o un
  ripristino. In entrambi i casi l'app non si apre più o sparisce.
- Rinnovo: rifare il passo 1 se la modalità sviluppatore è spenta; se il certificato
  distributore è scaduto, rinnovarlo nel *Certificate Manager* (stesso profilo, stesso
  certificato autore, stesso DUID); poi `scripts/tv-app.sh install <ip-della-tv>`. Con lo stesso
  certificato autore l'app si aggiorna e resta abbinata; con un certificato autore nuovo va prima
  disinstallata dalla TV e poi abbinata di nuovo con il codice.
- Prima della reinstallazione chiedere sempre l'ok su Telegram.

## Sicurezza (rischio dichiarato)

L'app parla con SMTrading in **http** sulla porta 8087 perché le app Tizen rifiutano il
certificato della rete di casa. Su quella porta c'è solo l'API delle schermate TV (sola lettura)
e l'abbinamento con codice, nient'altro del portale; rispondono solo gli indirizzi della rete di
casa (10.x, 172.16–31.x, 192.168.x). Il collegamento della TV viaggia in un'intestazione, mai
nell'indirizzo, e si revoca dal portale. La porta 8087 **non va aperta** sul router verso
Internet. Controllo della configurazione: `scripts/check-nginx-tv-app.sh` (Docker).

## Problemi comuni

| Messaggio | Cosa fare |
|---|---|
| «manca la riga di comando di Tizen» / «manca sdb» | installare Tizen Studio (punto «Cosa serve») o indicare `TIZEN_STUDIO` |
| «manca il profilo di certificato» | creare il profilo nel Certificate Manager (passo 2) |
| «la TV non risponde a sdb» | modalità sviluppatore accesa con l'indirizzo giusto del PC e TV riavviata (passo 1) |
| installazione rifiutata per il certificato | il DUID della TV deve essere nel certificato distributore (passo 2) |
| sulla TV: «Nessuna risposta da …» | SMTrading spento, porta 8087 non in linea o bloccata dal firewall |
| sulla TV: «Il server non ha dato il collegamento all'app» | l'indirizzo non è l'ingresso dell'app (porta 8087): tasto blu e correggerlo |
