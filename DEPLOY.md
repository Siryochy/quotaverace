# Deploy QuotaVerace

Il progetto si compone di due servizi:

1. **Backend** (bot Telegram **+** API JSON nello stesso processo) → **Railway**
2. **Frontend** (webapp Next.js) → **Vercel**

---

## 1. Backend su Railway

### Architettura (servizio unico)

Bot Telegram e API JSON girano **nello stesso container** ed entrypoint:
`run_all.py` avvia la web API (`web_api.py`) in un thread e poi il bot
(`bot.py`, long-polling) in primo piano. Un **unico volume** su `/app/data`
contiene DB, `data/scan_*.json`, log, cache e kill-switch: così persiste tutto
ed è condiviso per costruzione.

> ⚠️ **Railway non supporta volumi condivisi tra servizi separati**: ogni
> container avrebbe il proprio volume e i dati divergerebbero. Per questo bot e
> API devono stare nello **stesso** servizio. `Dockerfile.api` e il concetto di
> "secondo servizio API" sono superati e non devono essere usati.

### Setup

1. Crea un nuovo progetto su [Railway](https://railway.app) e collega questa
   repo GitHub.
2. Railway rileva il `Dockerfile` (`CMD ["python", "run_all.py"]`) che avvia
   bot + API insieme. L'infrastruttura è gestita via IaC in `.railway/railway.ts`:

   ```bash
   railway config apply --yes --confirm-destructive
   ```

3. Il volume `api-volume` viene creato/montato su `/app/data` (vedi
   **§1ter Volume**).

### Variabili d'ambiente (servizio api)

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `QUOTAVERACE_BOT_TOKEN` | ✅ | Token del bot Telegram (@BotFather). Se manca, `run_all.py` termina: il servizio resta in Crash |
| `ODDS_API_KEY` | opzionale | Chiave the-odds-api (quote live + notifiche) |
| `API_FOOTBALL_KEY` | opzionale | Chiave API-Football (rating dinamici + sync storico) |
| `BANKROLL_DEFAULT` | opzionale | Bankroll di default (default `100.0`) |
| `BETFAIR_APP_KEY` | opzionale | Application Key Betfair (serve per `/scan`; senza questa il bot segnala che Betfair non è configurato) |
| `BETFAIR_USERNAME` | opzionale | Username account Betfair Exchange (giurisdizione Italia) |
| `BETFAIR_PASSWORD` | opzionale | Password account Betfair Exchange |
| `BETFAIR_CERT_PATH` | opzionale | Percorso del certificato SSL client (pem: cert+key uniti) per il certlogin |
| `BETFAIR_CERT_KEY_PATH` | opzionale | Chiave privata separata (se non unita nel pem di `BETFAIR_CERT_PATH`) |
| `BETFAIR_DRY_RUN` | opzionale | `1` (default) = dry-run: nessun ordine reale. `0` = abilita la modalità live (con `BETFAIR_LIVE=1`) |
| `BETFAIR_LIVE` | opzionale | `0` (default) = solo simulazione. `1` = abilita ordini reali (solo insieme a `BETFAIR_DRY_RUN=0`) |
| `QUOTAVERACE_DATA_DIR` | opzionale | Directory dei dati persistenti (default `/app/data`). Su Railway punta al Volume montato; non usare `/app` |

> 💡 Tutte le `BETFAIR_*` sono opzionali: il bot funziona senza Betfair
> (i comandi `/scan` rispondono con le istruzioni di configurazione).
> Per configurazione e certificato, vedi **§1bis Integrazione Betfair**.

Questo è il **secondo servizio API non esiste più**: l'API è servita dallo
stesso container del bot sulla porta `PORT` iniettata da Railway.
`railway variable set --service api <KEY>=<value>` per gestirne i valori.

> ✅ **Persistenza dati**: tutti i dati (DB, `data/`, log, cache, kill-switch)
> vivono in `QUOTAVERACE_DATA_DIR` (default **`/app/data`**, accentrato in
> `config.DATA_DIR`). Il volume `api-volume` montato su `/app/data` preserva
> `quotaverace.db`, `data/scan_*.json`, `orders.jsonl`, `surebet_log.jsonl` e
> il kill-switch a ogni redeploy.
>
> ⚠️ **Monta il volume su `/app/data`, MAI su `/app`**: Railway **non usa
> overlay** — un volume sulla root `/app` nasconderebbe i sorgenti applicativi
> (vedi [docs Railway — Volumes](https://docs.railway.com/volumes)).
>
> 💡 Migrazione locale: se `quotaverace.db` era alla root del progetto, spostalo
> in `data/` (nuovo percorso) oppure imposta `QUOTAVERACE_DATA_DIR` al vecchio
> percorso prima del primo avvio.

---

## 1ter. Volume di persistenza

Un volume misura i dati persistenti di tutto l'app ed è dichiarato in
`.railway/railway.ts` (`api-volume`, 500 MB, montato su `/app/data`).
Montarlo su `/app/data` — **mai su `/app`**: Railway non usa overlay e un
volume sulla root nasconderebbe i sorgenti.

```bash
railway config plan              # anteprima
railway config apply --yes --confirm-destructive

# Stato volume e font: crea il pem con:
#   BETFAIR_CERT_PATH=/app/data/certs/client-ssl.cert.pem
railway volume list
railway volume files list / --json
```

> 💡 **UPsize** in live: da Hobby/Pro puoi ridimensionare il volume dalla
> dashboard senza downtime (Settings → live resize).

---

## 1bis. Integrazione Betfair (Exchange Italia)

Il client Betfair (`betfair_client.py`) parla con il Betfair Exchange via
JSON-RPC. È **fail-safe by design**:

- **Dry-run di default**: anche con tutte le credenziali impostate, nessun
  ordine reale parte finché NON hai entrambe `BETFAIR_DRY_RUN=0` **e**
  `BETFAIR_LIVE=1`. Il comando `/scan` del bot è solo lettura e non piazza
  mai ordini.
- **Kill-switch**: crea un file `data/kill_switch` per bloccare qualsiasi
  `placeOrders` (in live). Rimuovilo per riabilitare. Su Railway usa il Volume.
- Ogni ordine (reale o simulato) viene loggato su `data/orders.jsonl`.

### Requisiti Betfair

1. **Account Exchange Italia**: il client usa gli endpoint italiani
   (`identitysso-cert.betfair.it`) — serve un account giurisdizione IT.
2. **Application Key** (delayed key per l'automazione): richiedila da
   [developer.betfair.com](https://developer.betfair.com) → *Automated
   Access* → conferma l'accettazione delle condizioni d'uso.
3. **Certificato SSL client**: genera una coppia e unisci cert+key in un
   unico pem:

```bash
openssl genrsa -out client-ssl.key 2048
openssl req -new -x509 -key client-ssl.key -out client-ssl.crt -days 3650
cat client-ssl.crt client-ssl.key > client-ssl.cert.pem
# poi caricala nel profilo Betfair: My Account → Automated Betting Program
# Access → Upload SSL Client Certificate
```

4. **Whitelist IP**: l'accesso automatizzato richiede l'IP pubblico
   autorizzato nella dashboard Betfair (l'IP statico di Railway o l'egress
   del tuo servizio).

### Setup certificato su Railway

Railway non accetta upload di file: metti il pem sul **volume** montato su
`/app/data` e punta l'env lì:

```
BETFAIR_CERT_PATH=/app/data/certs/client-ssl.cert.pem
```

1. Il volume `api-volume` è già su `/app/data` (servizio unico, vedi **§1ter**).
2. Copia il pem nel volume (es. `railway run` + `scp`, oppure un commit
   temporaneo con un file non sensibile e poi spostalo nel volume —
   **mai** nel repo: la chiave privata NON deve finire su GitHub).

### Andare live (solo se sai cosa fai)

```
BETFAIR_DRY_RUN=0
BETFAIR_LIVE=1
```

Regole Exchange Italia già gestite dal client (validazione pre-lancio):

| Regola | Valore |
|---|---|
| Stake back minimo | 2.00 EUR |
| Step stake | multipli di 0.50 |
| Max istruzioni per `placeOrders` | 50 |
| Cap vincita per ordine | 10.000 EUR |

> ⚠️ In dry-run ogni ordine simula SUCCESS e viene loggato su
> `data/orders.jsonl`: usa i log per validare la strategia prima di
> passare a `BETFAIR_LIVE=1`.

---

## 2. Webapp su Vercel

1. Importa il progetto su [Vercel](https://vercel.com) con **Root Directory** = `webapp`.
2. Vercel rileva Next.js; usa `vercel.json` esistente.
3. Variabili d'ambiente:

| Variabile | Descrizione |
|---|---|
| `NEXT_PUBLIC_API_BASE` | URL pubblico del backend Railway, es. `https://quotaverace-backend.up.railway.app` |
| `BACKEND_URL` | (server-side) stesso URL del backend, per il proxy `/api/backend/:path*` in `next.config.js` |

4. Deploy. Le pagine Dashboard/Storico mostrano dati dimostrativi finché
   `NEXT_PUBLIC_API_BASE` non è impostata o il backend non risponde.

---

## 3. Verifica

```bash
# Backend
curl https://<backend-url>/api/health

# Frontend
curl https://<vercel-url>/api/backend/api/health   # via proxy
```

---

## 4. Note

- **Nuovo deployment**: quando riavvii il servizio, il volume resta montato
  e i dati persistono. Verifica con `railway logs` le righe
  `Mounting volume on: ...` e `QuotaVerace Pro avviato.`.
- **Rate limit**: il free plan di the-odds-api ha 500 req/mese; quello di
  API-Football 100 req/giorno. I job del bot sono già tarati per rientrare.
- **Betfair**: le chiamate Exchange non rientrano nei limiti the-odds-api;
  `listMarketCatalogue` max 1000 risultati/chiamata (il client usa 100 per
  tipo di mercato), `listMarketBook` max 200 market IDs per batch.
- **Long polling Telegram** funziona su Railway senza webhook; per webhook
  serve esporre una route HTTP dedicata.
- Il file `.env` locale non viene deployato: configura le variabili nella
  dashboard Railway/Vercel.
