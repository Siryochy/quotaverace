# AGENTS.md — Memoria di progetto (QuotaVerace)

> File di memoria per l'agente AI. Rileggere **all'inizio di ogni sessione** su
> questo progetto per ripartire senza perdere contesto. I documenti di
> riferimento sono: `DEPLOY.md` (infrastruttura), `STRATEGY.md` (strategia di
> gioco — **la priorità assoluta del progetto**), `README.md`.

## Cos'è QuotaVerace

Sistema di **value betting e surebet sul calcio**: motore di probabilità
(Poisson + Dixon-Coles con rating time-decay), confronto con le quote reali
dei bookmaker (odds API), filtro value (+EV), scanner arbitraggio, bot
Telegram, sito web e backtest. Obiettivo dichiarato del proprietario: **fare
profitto sulle scommesse** — ogni lavoro deve servire a migliorare l'edge.

## Architettura in 30 secondi

```
run_all.py          → avvia web_api.py (thread) + bot.py (long-polling, main)
bot.py              → bot Telegram (comandi, job schedulati, segnali)
web_api.py          → API JSON (senza framework, threading.HTTPServer)
tracker.py          → DB SQLite (schema + helper): segnali, analisi, cassa, ratings
fixture_engine.py   → analisi partite: modello vs mercato (CUORE STRATEGICO)
market_calib.py     → devigging + blend dinamico + CLV vig-free + longshot bias
ml_ensemble.py      → ensemble Poisson + Logistic Regression (numpy-only)
line_movement.py    → price snapshots, RLM detection, steam moves
bookmaker_advantage.py → soft book lag detection vs Pinnacle
adaptive_staking.py → Kelly frazionato dinamico + drawdown protection
rating_engine.py    → rating squadre time-decay (shrink usa COUNT reale `n`, NON `wsum`!)
poisson_engine.py   → modello Poisson/Dixon-Coles
value_filter.py     → gate EV + mercato, is_sane()
backtest.py         → calibrazione EV vs ROI, split "batte il mercato"
market_diagnose.py  → diagnosi calibrazione per mercato (ROI vs EV)
odds_ingest.py      → ingestione quote da odds API (cache in data/)
odds_api.py         → client API-Football (rate limit, quota giornaliera)
surebet_*.py        → scanner arbitraggio (Betfair ecc.)
daily_scanner.py    → job mattutino: partite del giorno + analisi
football_hist.py    → storico risultati (2022-2024) per le ratings
data/               → cache JSON scan + DB sqlite + modello ensemble
webapp/             → Next.js (Vercel): dashboard, cassa, schedina, calendario, backtest, value...
```

**Backend e bot stanno nello STESSO container Railway** (volume unico su
`/app/data`). Niente servizi separati con volumi divisi.

## Deploy (dettagli in DEPLOY.md)

| Cosa | Dove | Come |
|---|---|---|
| Backend (bot + API) | **Railway** | `git push` su main → auto-deploy (Dockerfile → `run_all.py`) |
| Volume persistente | Railway `api-volume` → `/app/data` | DB, cache, log, backup sopravvivono ai redeploy |
| Frontend webapp | **Vercel** | da `webapp/`: `vercel --prod` |
| Infrastruttura Railway | IaC in `.railway/railway.ts` | `railway config apply --yes --confirm-destructive` (⚠️ il CLI `railway` vive in `~/.npm-global`, NON cancellarlo nelle pulizie) |

- API base di produzione: `https://api-production-dffd.up.railway.app`
- Sito: `https://quotaverace.vercel.app`
- Variabili chiave: `QUOTAVERACE_BOT_TOKEN`, `API_FOOTBALL_KEY`, `RAILWAY_TOKEN`,
  `NEXT_PUBLIC_API_BASE` (nel progetto Vercel `quotaverace`).

## Comandi utili

```bash
venv/bin/python -m pytest -q          # test (240+)
venv/bin/python -c "..."              # script rapidi (usare venv/bin/python, NON python)
cd webapp && npm run build            # build Next.js
```

## Convenzioni e regole d'oro

1. **La strategia è l'elemento più importante** — prima di ogni feature, chiedersi
   se migliora l'edge. Ricerca web periodica su strategie 2026 (CLV, devigging,
   favourite-longshot bias).
2. **Il modello è corretto ORA** (fix 31/08): rating con shrink su `n` reale
   (il bug `wsum` collassava tutte le squadre a 1.0). Non "sistemare" la formula
   senza capire questo.
3. **Edge = battere il mercato devigato** (min +3pp value, +5pp strong_value),
   EV sulla probabilità blend modello+mercato. Vedere `STRATEGY.md`.
4. **Mai cancellare `~/.npm-global`** (contiene CLI railway e vercel).
5. **Python**: usare sempre `venv/bin/python`, mai `python` nudo.
6. **DB**: migrazioni schema con ALTER TABLE idempotente in `tracker.py`
   (già fatto per market_prob/market_edge) — verificare sul volume dopo il deploy.
7. **Token**: rotazione completata e verificata il **01/09** — GitHub con nuovo
   PAT fine-grained nel vault, Telegram con nuovo token su `api`/`production` e nel
   vault. Da ora in poi qualunque token finito in un canale pubblico va considerato
   compromesso e rotato subito (GitHub: nuovo PAT fine-grained → vault; Telegram:
   @BotFather → nuovo token → `railway variable set QUOTAVERACE_BOT_TOKEN`).

## Segreti: vault cifrato (`secrets/`)

- Tutti i segreti locali vivono in `secrets/vault.bin` (Fernet + PBKDF2,
  `SECRETS_MASTER_KEY` nel `.env` gitignored, chmod 600). Mai plaintext nel
  repo, mai loggati, caricati solo in memoria da `secrets_store.py` al
  bootstrap (`config.py` → `load_secrets_dir`).
- CLI: `venv/bin/python secrets_store.py vault|check|get NOME`. Per aggiungere
  un segreto: file plaintext in `secrets/` → `vault --commit` (cancella il
  plaintext). Se perdi `SECRETS_MASTER_KEY` senza plaintext, i segreti sono
  persi.
- Su Railway i segreti restano nelle env vars del progetto (cassaforte vera).

## Push automatico (credenziali GitHub)

- Il deploy è automatico: Railway ridistribuisce da solo a ogni push su `main`.
- Il push lo fa l'agente a fine lavoro con `GIT_ASKPASS=$(pwd)/.askpass_github.sh`
  (script gitignored che apre il VAULT e passa `GITHUB_TOKEN` a git senza mai
  stamparlo: env | .env → chiave maestra → `secrets_store.get_secret`).
- Il token va rinnovato quando scade o dopo l'esposizione in chat (flusso:
  fine-grained PAT → Contents RW → va nel VAULT, non più nel `.env`).

## Stato attuale (ultimo aggiornamento)

- **Deploy Railway**: Online, volume montato, bot in polling, health 200.
  **Consegna notifiche Telegram VERIFICATA** (test end-to-end 01/09):
  `POST /api/test_notify` con la chiave giusta ha consegnato il messaggio al
  Chat ID proprietario `7718157436` (ADMIN_CHAT_ID corretto su Railway).
  Endpoint protetto da `TEST_NOTIFY_KEY` (variabile Railway, mai nel repo).
  Bug storico fixato nello stesso giro: `format_schedina` mancava
  di `get_pro_stake` import → la schedina delle 08:00 non partiva con picks.
  **Verdetti puntate a fine partita**: `settle_bets(return_details=True)`
  restituisce i verdetti appena emessi e i job (pomeriggio/sera/23:30 e
  `/risultati`) inviano la notifica `🔔 ESITO PUNTATE AUTOMATICHE`
  (✅ VINTA/❌ PERSA/⚪ PUSH con P/L) a iscritti+admin.
- **Dataset ML** (`ml_dataset.py`): export CSV di addestramento da
  predictions+bets JOIN match_analysis (lam_h/lam_a, prob 1/X/2/O) e
  match_results → righe con label_ml (1=vinta). CLI
  `venv/bin/python ml_dataset.py` (→ data/training_dataset.csv) e
  `GET /api/training` (JSON, limit; es. ?limit=500).
- **Audit qualita' dataset ML** (`ml_audit.py`): un dataset sporco viene
  IMPARATO dal modello come verita' — controllo automatico di ogni riga
  (esito_finale valido, label_ml coerente, quota>1, prob in [0,1], profit
  col segno giusto, esiti strutturati per OU/AH/BTTS, duplicati). CLI
  `venv/bin/python ml_audit.py [--source predictions|bets]` (exit 0=ok,
  1=problemi) e **integrato nel report giornaliero**: `format_daily_report`
  audita le previsioni/puntate chiuse nel periodo e segnala i problemi
  (per tipo + primi esempi) in `/riepilogo` e nei report automatici.
- **Vault segreti**: attivo da locale (vault.bin Fernet/PBKDF2, 5 segreti
  cifrati, plaintext cancellati) — vedi sezione "Segreti".
- **Cassa**: funziona con doppia persistenza (localStorage + backup server sul
  volume). Endpoint: `GET/POST/DELETE /api/cassa`. **Ora si SALDA da sola**
  (`settle_cassa` in tracker.py): esito_finale/profit/settled_at, P/L reale e
  ROI in `/risultati` e nella pagina Cassa del sito.
- **Ledger previsioni** (tabella `predictions`): TUTTI i segnali proposti dal
  motore (1X2, Over/Under, Asian Handicap) vengono registrati con `mercato`,
  saldati a fine partita (`settle_predictions`, split-bet AH quarter incluso)
  e aggregati per mercato (`predictions_summary`) → telemetria di calibrazione
  in `/risultati`, `/backtest` e `/api/dashboard` (`per_mercato`).
- **Asian Handicap**: motore in `poisson_engine.ah_outcome_probs` (linee
  ±0.25…±3, push/split), parsing mercato `spreads` in fixture_engine
  (line shopping + devig power + blend + filtro EV). Solo telemetria per ora:
  il segnale della schedina resta 1X2/OU.
- **Quote**: fix fallback `load_odds(path)` (prima non funzionava mai) e nota
  di freschezza in `/segnale` quando le quote sono da cache vecchia.
- **Puntate automatiche** (`auto_bet.py`, job 08:50 ITA): piazza su Betfair
  Exchange i segnali value/strong_value del giorno (MATCH_ODDS e
  OVER_UNDER_25 dal catalogo di scansione), stake **ADATTIVO** (`adaptive_staking.py`):
  Kelly frazionato dinamico (0.10-0.35 vs 0.25 fisso prima) con drawdown
  protection (>10% drawdown → riduzione stakes) e confidence weighting
  (market_edge alto + strong_value → stake più alto). Cap: 3% value, 5%
  strong_value. Fallback: stake fisso `BET_STAKE_EUR` se modulo assente.
  **DRY-RUN di default**: ordini reali solo con `BETFAIR_DRY_RUN=0` +
  `BETFAIR_LIVE=1` e senza kill-switch (`data/kill_switch`). Guardie:
  salta partite a <15 min dall'inizio, prezzi Exchange <95% della quota
  segnale, doppie puntate (UNIQUE match_id+esito).
  **Verifica incrociata runner**: `_resolve_team` risolve gli alias squadre
  (TEAM_MAP: 'AC Milan'→'milan', 'West Ham United'→'west ham') sia sul
  matching dell'evento sia sull'esito; `_runner_esito` accetta SOLO runner
  riconosciuti (draw o una delle due squadre) — mai un runner ambiguo
  (fail-closed: la partita non si perde per un alias, ma un runner non
  riconosciuto non viene mai piazzato). Registro in tabella `bets`, saldato
  a fine partita (`settle_bets`) e incluso nel riepilogo.
- **Report giornaliero**: `/riepilogo [oggi|ieri|YYYY-MM-DD]` + invio
  automatico all'alba (06:05 ITA, riepilogo di ieri) e **a fine ultima
  partita** (check ogni 15' dalle 21:00 ITA, fallback notturno 23:50 ITA):
  previsioni chiuse per mercato (ROI vs EV), cassa saldata, puntate auto
  (P/L), CLV raw + **CLV vig-free** (devigato, piu' accurato) + CLV vs
  Pinnacle (closing line sharp), e alert chiavi mancanti.
  **Timezone**: i job usano UTC; `IT_OFFSET=2` converte gli orari in italiani
  (cambiare a 1 a fine ottobre per ora legale invernale).
  Destinatari: iscritti (`/subscribe`) **+ sempre** i chat in `ADMIN_CHAT_ID`
  (proprietario, virgola-separati). `/myid` mostra il proprio Chat ID.
- **Sticker premium**: inviato prima dei messaggi premium (set pubblico
  `PREMIUM_STICKER_SET`, default "Diamond") — workaround gratis alle custom
  emoji (che richiederebbero Fragment o Premium sull'account proprietario).
- **Copertura MONDIALE (66 competizioni)**: SPORTS_MAP (odds_api.py)
  interroga TUTTE le competizioni di calcio the-odds-api (chiavi ufficiali
  verificate sul sito): top campionati + serie B + coppe europee/internaz.
  + nazionali.  **Rotazione crediti piano free** (500 crediti/mese, reset il 1°):
  SPORTS_INTERVAL_DAYS calibrato su ~407/mese (top leghe ogni 2gg, coppe
  ogni 3gg, resto ogni 7/14/30gg) + **finestra QUERY_WINDOW_DAYS=7** (una
  chiamata copre l'intera settimana: nessuna partita persa anche con
  rotazioni rade) + **cap giornaliero DAILY_QUERY_BUDGET** (default 12,
  env `ODDS_DAILY_BUDGET`): le leghe in eccedenza sono rinviate al giorno
  dopo (log warning). Costo mensile verificato dal test
  test_budget_mensile_piano_free (<= 460). **Squadre fuori roster NON
  vengono piu' saltate**: `_match_team` ritorna il nome API e
  `expected_goals` usa il profilo di lega di default (i rating reali
  arrivano coi risultati). Chiave Brasileirao corretta:
  `soccer_brazil_campeonato`. Test: test_odds_api.py.
- **Partite saltate MAI silenziose**: fetch_and_analyze_today traccia le
  partite trovate ma non analizzate → saltate.json + `/api/analisi` (campo
  `saltate`) + sezione nel report. Con la copertura mondiale il campo e'
  vuoto per design (ogni partita e' analizzata).
- **Webapp**: 8 sezioni live (Dashboard, Calcola, Schedina, Storico, Cassa,
  Calendario, Backtest, Value).
- **Test**: 327+ test verdi (la suite completa richiede ~4 min).

## Moduli avanzati (Settembre 2026)

- **ML Ensemble** (`ml_ensemble.py`): Logistic Regression numpy-only che
  combina le probabilità Poisson con un classificatore addestrato sul
  dataset storico. Peso dinamico basato sul Brier score. Save/load in
  `data/ensemble_model.json`. Integrato in `fixture_engine._analyze_match`.
- **Line Movement Tracking** (`line_movement.py`): tabella `price_snapshots`
  registra i prezzi ad ogni analisi. RLM detection (reverse line movement =
  segnale sharp money quando il prezzo si muove contro il pubblico) e steam
  move detection (movimento > 6% in < 30 min). CLI per analisi.
- **Bookmaker Advantage** (`bookmaker_advantage.py`): confronta quote Pinnacle
  (sharp) con i soft book. Rileva lag (soft book non aggiornato) e calcola
  l'edge aggiuntivo dal lag. Integra in `fixture_engine`.
- **Adaptive Staking** (`adaptive_staking.py`): Kelly frazionato dinamico
  (0.10-0.35) con confidence weighting (market_edge, ML confidence, CLV,
  status) e drawdown protection (>10% → riduzione stakes). Integrato in
  `auto_bet.py` (ogni puntata ha stake diverso).
- **Dynamic Blend** (`market_calib.py`): `blend_probability()` ora accetta
  `league`, `odds`, `model_samples` per calcolare il peso dinamico.
  `LEAGUE_EFFICIENCY` con score per 30+ leghe (Premier League 0.85 →
  Indian Super League 0.35). Mercato efficiente → peso modello basso.
- **CLV Vig-Free** (`market_calib.py`): `clv_vig_free()` calcola CLV sulla
  closing line devigata (non la quota grezza). Corregge la sovrastima del
  CLV tradizionale. Il report mostra CLV raw, vig-free e vs Pinnacle.
- **Market Diagnose** (`market_diagnose.py`): diagnosi calibrazione per
  mercato. Confronta ROI realizzato vs EV atteso, identifica mercati
  critici (gap >= 3pp) e suggerisce tuning (blend, devig, soglia EV).
- **Fix Timezone Job**: tutti i job Telegram ora usano `IT_OFFSET=2` per
  convertire UTC → ora italiana. Prima il report delle 23:50 partiva
  alle 01:50 italiane!
- **Test**: 327+ test verdi (la suite completa richiede ~4 min).
- **Sicurezza**: rotazione token completata e verificata il 01/09 — nuovo GitHub
  PAT nel vault, nuovo token Telegram (`@Calcifrrbot`, ID 8372645521) attivo su
  `api`/`production`: `getMe` 200 nel deployment 2bf814fb, test notifica
  consegnato al Chat ID proprietario 7718157436.
- **Sicurezza**: rotazione token completata e verificata il 01/09 — nuovo GitHub
  PAT nel vault, nuovo token Telegram (`@Calcifrrbot`, ID 8372645521) attivo su
  `api`/`production`: `getMe` 200 nel deployment 2bf814fb, test notifica
  consegnato al Chat ID proprietario 7718157436.

## Prossimi passi possibili (non urgenti)

- Quando il ledger avrà 100+ previsioni chiuse: usare `market_diagnose.py`
  per identificare mercati critici e ajustare blend/devig/soglie.
- Mostrare RLM/steam nel report Telegram e nella webapp.
- Integrazione XGBoost quando il dataset ML raggiunge 500+ campioni
  (attualmente Logistic Regression numpy-only per evitare deps pesanti).
- Cambiare `IT_OFFSET` da 2 a 1 a fine ottobre (ora legale invernale).
