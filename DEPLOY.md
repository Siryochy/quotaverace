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
> proprietario): l'ESECUZIONE passa ora da un aggregatore professionale via
> `execution_engine.py` (BetInAsia BLACK / MollyBet, protocollo
> Betfair-compatible). Credenziali aggregatore SOLO da env
> (`EXECUTION_APP_KEY`, `EXECUTION_USERNAME`, `EXECUTION_PASSWORD`).
> **Refertazione risultati = the-odds-api** (`odds_api.fetch_scores`, la
> stessa chiave delle quote restituisce i risultati finiti della stagione
> corrente); quote/CLV = the-odds-api. API-Football serve SOLO allo storico
> ratings 2022-2024 (`football_hist.py`): il piano free NON dà accesso alla
> stagione corrente (verificato 04/09), quindi non può saldare le partite
> del 2026. `auto_bet` è SIM-only (collegamento a execution_engine: in
> programma).

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

## 1bis. Esecuzione via aggregatore (dal 06/09)

Il tripwire Betfair è stato rimosso il 06/09 (decisione del proprietario):
l'esecuzione passa dagli **aggregatori professionali** per evitare
limitazioni e ottimizzare le quote. Il modulo `execution_engine.py` espone
un'unica interfaccia Python verso **BetInAsia BLACK / MollyBet** (protocollo
Betfair-compatible JSON-RPC, SportsAPING/v1.0) con:
- credenziali SOLO da env: `EXECUTION_APP_KEY`, `EXECUTION_USERNAME`,
  `EXECUTION_PASSWORD` (mai hardcoded — vault locale + env Railway);
- `DryRunProvider` di default (nessuna rete) quando mancano le credenziali;
- probe a stake minimo (`EXECUTION_MIN_STAKE_EUR`, default 1€) che misura
  latenza e slippage reali e li logga in `data/execution/measurements.jsonl`.

Uso:
```bash
venv/bin/python execution_engine.py --status
venv/bin/python execution_engine.py --probe --market <id> --selection <id>
```

Architettura attuale:
- **Refertazione**: the-odds-api (`odds_api.fetch_scores` +
  `match_scores_by_name`) — risultati finiti della stagione corrente, 1
  credito per sport, aggancio ai match_id the-odds-api già in `matches`.
- **Quote + CLV**: the-odds-api (`odds_api.py`).
- **Puntate automatiche**: SIM-only (paper trading) — collegamento a
  `execution_engine` in programma dopo il collaudo con stake minimo.
- **Storico ratings**: API-Football (`football_hist.py`, stagioni 2022-2024
  coperte dal piano free).
- **Mercati**: SOLO 1X2 — OU2.5 escluso definitivamente (06/09, leak
  sistematico, nessun escape hatch).

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
- **Esecuzione (dal 06/09)**: aggregatore via `execution_engine.py`
  (BetInAsia BLACK / MollyBet), credenziali da env, probe 1€ per
  latenza/slippage. Refertazione esclusivamente the-odds-api (fetch_scores
  della stagione corrente), quote/CLV the-odds-api, API-Football solo
  storico ratings 2022-2024.
- **Long polling Telegram** funziona su Railway senza webhook; per webhook
  serve esporre una route HTTP dedicata.
- Il file `.env` locale non viene deployato: configura le variabili nella
  dashboard Railway/Vercel.
