# Deploy QuotaVerace

Il progetto si compone di due servizi:

1. **Backend** (bot Telegram + API JSON) → **Railway**
2. **Frontend** (webapp Next.js) → **Vercel**

---

## 1. Backend su Railway

### Setup

1. Crea un nuovo progetto su [Railway](https://railway.app) e collega questa repo GitHub.
2. Railway rileva il `Dockerfile` e userà `railway.toml` (start: `python bot.py`).

### Variabili d'ambiente (servizio bot)

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `QUOTAVERACE_BOT_TOKEN` | ✅ | Token del bot Telegram (@BotFather) |
| `ODDS_API_KEY` | opzionale | Chiave the-odds-api (quote live + notifiche) |
| `API_FOOTBALL_KEY` | opzionale | Chiave API-Football (rating dinamici + sync storico) |
| `BANKROLL_DEFAULT` | opzionale | Bankroll di default (default `100.0`) |

### Servizio API JSON

Crea un **secondo servizio** nello stesso progetto Railway, con le stesse variabili più:

- **Builder**: Dockerfile → imposta `Dockerfile.api`
- **Start command**: `python web_api.py` (il `Dockerfile.api` lo imposta già)
- Railway inietta `PORT` automaticamente: `web_api.py` lo legge (fallback `WEB_API_PORT`, poi `8000`).
- Apri la porta pubblicata e copia l'URL pubblico (es. `https://quotaverace-backend.up.railway.app`).

> ⚠️ **Persistenza dati**: `quotaverace.db` è un SQLite locale al container.
> Su Railway il filesystem è effimero: monta un **Volume** su `/app` (o usa un
> database gestito) se vuoi che storico e risultati sopravvivano ai redeploy.

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

- **Rate limit**: il free plan di the-odds-api ha 500 req/mese; quello di
  API-Football 100 req/giorno. I job del bot sono già tarati per rientrare.
- **Long polling Telegram** funziona su Railway senza webhook; per webhook
  serve esporre una route HTTP dedicata.
- Il file `.env` locale non viene deployato: configura le variabili nella
  dashboard Railway/Vercel.
