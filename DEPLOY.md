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
| `ODDS_API_KEY` | opzionale | Chiave the-odds-api (quote + CLV) |
| `API_FOOTBALL_KEY` | opzionale | Chiave API-Football (solo storico ratings 2022-2024: il piano free NON copre la stagione corrente) |
| `BANKROLL_DEFAULT` | opzionale | Bankroll di default (default `100.0`) |
| `QUOTAVERACE_DATA_DIR` | opzionale | Directory dei dati persistenti (default `/app/data`). Su Railway punta al Volume montato; non usare `/app` |

> ⚠️ **Il tripwire Betfair è stato rimosso il 06/09** (decisione del
> proprietario): l'ESECUZIONE passa ora da un provider professionale via
> `execution_engine.py` — **SX Bet** (V3 crypto, dal 07/09), **Smarkets**
> (API REST v3, dal 07/09) oppure aggregatori Betfair-compatible (BetInAsia
> BLACK / MollyBet). Credenziali SOLO da env: `SX_API_KEY`/`SX_PRIVATE_KEY`
> per SX Bet (richiede `eth-account` in requirements.txt), `SMARKETS_USERNAME`/
> `SMARKETS_PASSWORD` per Smarkets,
> `EXECUTION_APP_KEY`/`EXECUTION_USERNAME`/`EXECUTION_PASSWORD` per gli
> aggregatori.
> **Refertazione risultati = the-odds-api** (`odds_api.fetch_scores`, la
> stessa chiave delle quote restituisce i risultati finiti della stagione
> corrente); quote/CLV = the-odds-api. API-Football serve SOLO allo storico
> ratings 2022-2024 (`football_hist.py`): il piano free NON dà accesso alla
> stagione corrente (verificato 04/09), quindi non può saldare le partite
> del 2026. `auto_bet`: SIM di default, ordini **LIVE** via
> `execution_engine` dal 08/09 con `AUTO_BET_MODE=live` + provider reale
> (`EXECUTION_PROVIDER` + credenziali; oggi SX Bet).

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

# Stato volume
railway volume list
railway volume files list / --json
```

> 💡 **UPsize** in live: da Hobby/Pro puoi ridimensionare il volume dalla
> dashboard senza downtime (Settings → live resize).

---

## 1bis. Esecuzione via provider (dal 06/09; Smarkets 07/09, SX Bet 07/09)

Il tripwire Betfair è stato rimosso il 06/09 (decisione del proprietario):
l'esecuzione passa da **SX Bet**, **Smarkets** o dagli **aggregatori
professionali**. Il modulo `execution_engine.py` espone un'unica interfaccia
Python (`ExecutionProvider`) con:
- **SX Bet** (`EXECUTION_PROVIDER=sxbet`, dal 07/09): exchange P2P crypto
  su **SX Rollup** (Arbitrum Orbit, chainId 4162), API `https://api.sx.bet`
  (testnet: `SX_API_BASE=https://api.toronto.sx.bet`). La **V3** è live dal
  26/08/2026 (la V2 non esiste più): letture pubbliche senza chiave
  (markets/book/metadata), scritture con header `x-sx-api-key`, ordini
  firmati **EIP-712** con la chiave privata dell'EOA `SX_PRIVATE_KEY`.
  Credenziali SOLO da env: `SX_API_KEY`/`SX_PRIVATE_KEY` (richiede
  `eth-account` in requirements.txt, import lazy). Il calcio 1X2 è il
  market type 1, decomposto in 3 mercati binari "X vs Not X"
  (Home/Tie/Away): selection 1 = esito X, 2 = "Not X". `percentageOdds` =
  probabilità ×1e20 (ladder 0.125%), `totalBetSize` in unità USDC (6
  decimali), timeInForce IOC/FOK = take immediato, GTC = resta sul book.
  ⚠️ I fondi stanno nel **proxy wallet** dell'account (deploy + funding via
  UI sx.bet o `POST /user/deploy-proxy`), NON nell'EOA: il probe reale va
  fatto dopo il deposito. Raggiungibile dall'Italia (verificato 07/09),
  nessun blocco ADM.
- **Smarkets** (`EXECUTION_PROVIDER=smarkets`, dal 07/09): API REST pubblica
  v3 (`https://api.smarkets.com/v3/`), credenziali SOLO da env
  `SMARKETS_USERNAME`/`SMARKETS_PASSWORD` (base URL personalizzabile con
  `SMARKETS_API_BASE`). Protocollo dal sample ufficiale `smk_trading_bot`:
  login `POST sessions/` → header `Authorization: Session-Token <token>`;
  prezzi in probabilità ×1e4 (5000 = quota 2.0), quantità in stake ×1e4;
  side `buy`=BACK / `sell`=LAY; mercati 1X2 calcio = event type
  `football_match`, market type `match_odds`, contratti Home/Draw/Away
  (il contract_id fa da selection_id).
- **BetInAsia BLACK / MollyBet** (protocollo Betfair-compatible JSON-RPC,
  SportsAPING/v1.0): credenziali SOLO da env `EXECUTION_APP_KEY`,
  `EXECUTION_USERNAME`, `EXECUTION_PASSWORD` (mai hardcoded — vault locale
  + env Railway).
- `DryRunProvider` di default (nessuna rete) quando mancano le credenziali;
- probe a stake minimo (`EXECUTION_MIN_STAKE_EUR`, default 1€) che misura
  latenza e slippage reali e li logga in `data/execution/measurements.jsonl`;
- discovery mercati (`--markets`): elenca i match odds calcio aperti
  (finestra −1h/+48h, ordinati per kickoff) con i selection/contract id,
  così il probe si lancia senza cercare i market id a mano.

Uso:
```bash
venv/bin/python execution_engine.py --status
venv/bin/python execution_engine.py --markets [--max 20]

# SX Bet (credenziali da env; discovery e book sono pubblici senza chiave)
venv/bin/python execution_engine.py --provider sxbet --markets --max 10
SX_API_KEY=... SX_PRIVATE_KEY=0x... \
  venv/bin/python execution_engine.py --provider sxbet --probe \
  --market <marketHash_hex> --selection 1 [--price 2.0]

# Smarkets (credenziali da env)
SMARKETS_USERNAME=... SMARKETS_PASSWORD=... \
  venv/bin/python execution_engine.py --provider smarkets --probe \
  --market <market_id> --selection <contract_id> [--price 2.0]

# Aggregatore Betfair-compatible
venv/bin/python execution_engine.py --probe --market <id> --selection <id>
```

> ⚠️ **Proxy wallet SX Bet**: prima del primo ordine reale va deployato il
> proxy (wizard di sx.bet o `POST /user/deploy-proxy`) e finanziato con
> USDC sulla chain SX Rollup (4162). Senza proxy gli ordini vengono
> rifiutati anche con API key e chiave valide.

> ⚠️ **Protocolli diversi tra provider**: SX Bet espone la REST V3 con
> ordini firmati EIP-712 (implementato, V3 live dal 26/08/2026); Smarkets
> espone l'API REST v3 (implementato); BetInAsia BLACK espone un'API
> Betfair-compatible (JSON-RPC SportsAPING/v1.0, come implementato);
> MollyBet ha un protocollo REST proprietario su `api.mollybet.com`
> (sessioni/stream/betslip) — prima di puntare su MollyBet serve un client
> dedicato. Verificare con l'account quale interfaccia espone davvero.
>
> ⚠️ **Blocco ADM in Italia**: `api.smarkets.com` è tra i domini inibiti
> dall'Agenzia delle Dogane e dei Monopoli (DNS verso il blocco SOGEI
> `sito-inibito-giochi.adm.gov.it`) — da reti italiane il provider Smarkets
> non è raggiungibile. Il job di esecuzione va eseguito da Railway o da una
> rete non soggetta all'inibizione. I test del modulo sono tutti mockati
> (nessuna dipendenza dalla rete).

Architettura attuale:
- **Refertazione**: the-odds-api (`odds_api.fetch_scores` +
  `match_scores_by_name`) — risultati finiti della stagione corrente, 1
  credito per sport, aggancio ai match_id the-odds-api già in `matches`.
- **Quote + CLV**: the-odds-api (`odds_api.py`).
- **Puntate automatiche**: SIM di default (paper trading) oppure **LIVE**
  via `execution_engine` (08/09) con `AUTO_BET_MODE=live` + provider reale
  configurato (SX Bet): risoluzione evento univoca + floor EV (riempimento
  solo a quota-segnale o meglio), ledger `bets` con mode='live' e
  market_id/bet_id reali. Senza provider configurato resta SIM (fail-safe).
- **Storico ratings**: API-Football (`football_hist.py`, stagioni 2022-2024
  coperte dal piano free).
- **Mercati**: SOLO 1X2 — OU2.5 escluso definitivamente (06/09, leak
  sistematico, nessun escape hatch).

> 💡 Per attivare l'esecuzione reale del job 08:50 su Railway:
> `railway variable set --service api AUTO_BET_MODE=live EXECUTION_PROVIDER=sxbet`
> (credenziali `SX_API_KEY`/`SX_PRIVATE_KEY` già presenti). `AUTO_BET_MODE`
> assente = simulazione. Fail-closed: se il chiamante passa allow_sim=False
> e il provider non è configurato non si piazza nulla.

> ⚠️ Le regole di stake (minimo 2.00 EUR, step 0.50) sono mantenute in
> `auto_bet.normalize_stake` per coerenza con le dimensioni storiche.

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
- **Esecuzione (dal 06/09, Smarkets dal 07/09)**: provider via
  `execution_engine.py` (Smarkets REST v3 oppure BetInAsia BLACK /
  MollyBet Betfair-compatible), credenziali da env, probe 1€ per
  latenza/slippage. Refertazione esclusivamente the-odds-api (fetch_scores
  della stagione corrente), quote/CLV the-odds-api, API-Football solo
  storico ratings 2022-2024.
- **Long polling Telegram** funziona su Railway senza webhook; per webhook
  serve esporre una route HTTP dedicata.
- Il file `.env` locale non viene deployato: configura le variabili nella
  dashboard Railway/Vercel.
