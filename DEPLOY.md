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
| `API_FOOTBALL_KEY` | opzionale | Chiave API-Football (settlement risultati + rating dinamici + sync storico) |
| `BANKROLL_DEFAULT` | opzionale | Bankroll di default (default `100.0`) |
| `QUOTAVERACE_DATA_DIR` | opzionale | Directory dei dati persistenti (default `/app/data`). Su Railway punta al Volume montato; non usare `/app` |

> ⚠️ **Betfair è stato RIMOSSO dall'architettura il 04/09**: nessuna
> variabile `BETFAIR_*` è più necessaria e i relativi moduli non esistono.
> La refertazione usa esclusivamente API-Football (`settlement_apifootball.py`),
> le quote/CLV esclusivamente the-odds-api. `auto_bet` è SIM-only.

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

## 1bis. (RIMOSSO 04/09) Integrazione Betfair

Betfair è stato **escluso definitivamente dall'architettura il 04/09** per
eliminare la dipendenza dall'account Exchange. Moduli rimossi:
`betfair_client.py`, `daily_scanner.py`, `daily_scan_job.py`,
`surebet_pipeline.py` (+ test dedicati). Comando `/scan` e endpoint
`/api/scan` (risponde "betfair_removed") eliminati; health mostra
`betfair_enabled: false` per compatibilità frontend.

Architettura attuale:
- **Refertazione**: API-Football (`settlement_apifootball.py`) — fixtures
  finite abbinate per nome ai match_id the-odds-api, mappa lega→league_id
  ~50 campionati. Limite free plan: 100 richieste/giorno (si scaricano solo
  le leghe con match/cassa aperti).
- **Quote + CLV**: the-odds-api (`odds_api.py`).
- **Puntate automatiche**: SIM-only (paper trading con la quota del segnale).

> ⚠️ **Requisiti Betfair e setup certificato sono stati RIMOSSI il 04/09**
> insieme all'integrazione: nessuna credenziale/certificato Exchange è più
> necessaria. Le regole di stake (minimo 2.00 EUR, step 0.50) sono mantenute
> in `auto_bet.normalize_stake` per coerenza con le dimensioni storiche.

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
- **Betfair (rimosso 04/09)**: nessuna chiamata Exchange — refertazione
  esclusivamente API-Football (100 req/giorno free plan: il settlement
  scarica solo le leghe con match/cassa aperti), quote/CLV the-odds-api.
- **Long polling Telegram** funziona su Railway senza webhook; per webhook
  serve esporre una route HTTP dedicata.
- Il file `.env` locale non viene deployato: configura le variabili nella
  dashboard Railway/Vercel.
