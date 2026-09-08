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
probability_calibration.py → calibrazione isotonica PAVA numpy-only dell'ensemble
line_movement.py    → price snapshots, RLM detection, steam moves
bookmaker_advantage.py → soft book lag detection vs Pinnacle
adaptive_staking.py → Kelly frazionato dinamico + drawdown protection
secure_logging.py   → filtro log: maschera segreti/token, httpx silenzioso
rating_engine.py    → rating squadre time-decay (shrink usa COUNT reale `n`, NON `wsum`!)
poisson_engine.py   → modello Poisson/Dixon-Coles
value_filter.py     → gate EV + mercato, is_sane()
backtest.py         → calibrazione EV vs ROI, split "batte il mercato"
market_diagnose.py  → diagnosi calibrazione per mercato (ROI vs EV)
odds_ingest.py      → ingestione quote da odds API (cache in data/)
odds_api.py         → client the-odds-api (rate limit, quota giornaliera) — quote E CLV
football_hist.py    → storico risultati (2022-2024, piano free API-Football) per le ratings
surebet_scanner.py  → prototipo scanner arbitraggi (solo mock, NON in produzione)
surebet_engine.py   → scanner arbitraggi INDIPENDENTE (05/09): h2h a 2 esiti
                      NBA/MLB/Tennis, soft vs sharp via the-odds-api, cache e
                      log propri (data/surebet/), mai import da tracker/bot
                      (loop separato: venv/bin/python surebet_engine.py --loop N)
tennis_sandbox.py   → SANDBOX tennis (08/09): paper trading Moneyline 2 vie su
                      SX Bet (type 52), baseline Weighted ELO seminata dal
                      mercato, +EV con anti-spurio (inv_sum 0.98-1.08), ledger
                      SQLite dedicato (data/tennis_sandbox/), ZERO ordini reali
                      e zero crediti the-odds-api (letture SX pubbliche)
data/               → cache JSON + DB sqlite + modello ensemble
backtest_mc.py      → backtest walk-forward ensemble+Kelly con Monte Carlo (ROI, MaxDD)
backup_manager.py   → backup DB+dataset ML (integrity check, rotazione, /backup)
execution_engine.py → ESECUZIONE ordini via provider (SX Bet V3 crypto / Smarkets /
                      BetInAsia BLACK / MollyBet): provider interface + DryRun +
                      probe a stake minimo (1€) latenza/slippage
webapp/             → Next.js (Vercel): dashboard, cassa, schedina, calendario, backtest, value...
```

**Il tripwire Betfair è stato RIMOSSO il 06/09**: i moduli `betfair_client.py`,
`daily_scanner.py`, `daily_scan_job.py`, `surebet_pipeline.py` non esistono più,
ma la direzione è cambiata — l'ESECUZIONE passa ora da un provider di
`execution_engine.py` (**SX Bet** V3 crypto dal 07/09, **Smarkets** dal
07/09, o gli aggregatori BetInAsia BLACK / MollyBet Betfair-compatible).
Refertazione = the-odds-api (`odds_api.fetch_scores`,
risultati stagione corrente — la stessa chiave delle quote); quote/CLV =
the-odds-api. API-Football serve SOLO allo storico ratings 2022-2024
(`football_hist.py`): il piano free NON copre la stagione corrente (verificato
04/09), quindi non può saldare le partite del 2026. auto_bet: SIM di
DEFAULT, ordini LIVE via `execution_engine` dal 08/09 con
`AUTO_BET_MODE=live` + provider reale configurato (EXECUTION_PROVIDER +
credenziali; oggi SX Bet). Il vincolo "settlement = the-odds-api" resta garantito da
`test_settlement_source.py`; il vincolo "nessuna credenziale in chiaro" da
`test_secret_hygiene.py`.

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
- Shell sul container: `railway ssh --service api` (chiave SSH locale
  `~/.ssh/id_ed25519` registrata come "quotaverace-debug"; nel container NON
  c'è la CLI `sqlite3` → interrogare il DB con `python3 -c "..."`).
- Variabili chiave: `QUOTAVERACE_BOT_TOKEN`, `API_FOOTBALL_KEY`, `RAILWAY_TOKEN`,
  `NEXT_PUBLIC_API_BASE` (nel progetto Vercel `quotaverace`).

## Comandi utili

```bash
venv/bin/python -m pytest -q          # test (481+, ~5 min)
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
7. **Token**: rotazioni completate e verificate il **01/09**, il **02/09** e
   il **04/09** (Telegram attivo su `api`/`production`, `getMe` 200).
   ⚠️ Il token del 02/09 (incollato in chat) è stato REVOCATO il 04/09 via
   Opzione A: il vecchio risponde 401, il nuovo è attivo e nel vault cifrato
   locale. Regola permanente: qualunque token finito in un canale pubblico
   va considerato compromesso e rotato subito, SENZA incollarlo in chat
   (Opzione A: utente genera da @BotFather e fa `railway variables --service
   api --set QUOTAVERACE_BOT_TOKEN=...`; l'agente verifica e fa il merge nel
   vault senza mai vedere il valore — GitHub: PAT fine-grained → vault;
   Telegram: @BotFather). Vincolo custodito dal tripwire
   `test_secret_hygiene.py` (rompe se compare una credenziale in chiaro).

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
- `vault --commit` NON è ricorsivo: considera solo i file diretti in
  `secrets/` (`iterdir`) — le sottocartelle non vengono toccate (es. la
  vecchia `secrets/betfair/` col cert SSL, ora inutile e ignorabile). MAI
  mettere `*.key`/`*.pem` direttamente in `secrets/`: verrebbero trattati
  come segreti e cancellati dal commit.
- **Tripwire igiene segreti** (`test_secret_hygiene.py`): la suite ROMPE se un
  sorgente .py contiene una credenziale in chiaro (formati noti: token
  Telegram, GitHub PAT, Google API key, AWS, Slack, Stripe, PEM, Bearer;
  assegnazioni a nomi credential-like; URL user:password) o se `.env*`/
  `secrets/` finiscono tracciati in git. Tutte le chiavi si leggono SOLO da
  env (`.env` gitignored + vault): audit completato con zero valori hardcoded.
  Rimossa da `test_secure_logging.py` una credenziale Telegram REALE ma
  rotata (era finita lì come esempio nel fix del 01/09): lo scrub maschera
  sul FORMATO, quindi ora il test usa un fake marcato `fake/test`.

## Push automatico (credenziali GitHub)

- Il deploy è automatico: Railway ridistribuisce da solo a ogni push su `main`.
- Il push lo fa l'agente a fine lavoro con `GIT_ASKPASS=$(pwd)/.askpass_github.sh`
  (script gitignored che apre il VAULT e passa `GITHUB_TOKEN` a git senza mai
  stamparlo: env | .env → chiave maestra → `secrets_store.get_secret`).
- Il token va rinnovato quando scade o dopo l'esposizione in chat (flusso:
  fine-grained PAT → Contents RW → va nel VAULT, non più nel `.env`).

## Stato attuale (aggiornato al 08/09/2026)

- **Sandbox tennis + scansione calcio H24/7 (08/09)** — nuovo modulo
  `tennis_sandbox.py` (pattern surebet_engine: indipendente da tracker/bot,
  ledger SQLite dedicato in `data/tennis_sandbox/`, mai import da tracker/bot
  e nessuna credenziale — test dedicati lo verificano). Legge i mercati
  tennis Moneyline (type 52 = "12", sportId 6) dall'API PUBBLICA di SX Bet
  (zero costi, zero ordini: fail-closed totale). Baseline **Weighted ELO**
  (K pesato per recency, seeding a COPPIA coerente — il seeding singolo
  distorceva prob: 0.773→0.920) seminata dalle probabilita' implicite del
  mercato e aggiornata SOLO dai risultati reali delle osservazioni saldate
  (`/markets/find` → `outcome` 1|2|0). Anti-EV-spurio: filtro coerenza
  mercato `MIN_INV_SUM=0.98/MAX_INV_SUM=1.08` (somma inversi dei due best
  back ~1 su un exchange; sotto soglia = book sporco, l'EV finto supera
  l'EV_MIN 3% solo con edge reale del modello). Ledger: ogni +EV registrato
  con selezione univoca (UNIQUE market_hash+selection), stake Kelly
  frazionario su bankroll VIRTUALE (paper, default 1000) e OGNI match
  scansionato salvato come observation per l'apprendimento ELO (senza
  settlement l'ELO resterebbe identico al mercato: deadlock). Report
  `--report [--json]`: opportunita'/giorno, ROI teorico, win rate. CLI:
  `--scan`, `--settle`, `--loop N`. Attivo su Railway con
  `TENNIS_SANDBOX_ENABLED=1` (scan+settle ogni 6h + report 05:55 UTC via
  Telegram); collaudo reale 08/09: 8 mercati US Open, 8 book, 1 segnale +EV
  (Linda Noskova @4.21 vs rating seminato — mercato in movimento).
- **Scansione calcio H24/7 garantita da test** (08/09): il percorso 1X2
  (bot.py morning/afternoon/evening + fixture_engine + odds_api) non ha
  alcun gating per giorno della settimana — tripwire
  `test_football_scan_h24.py` blocca chiunque introduca limitazioni "solo
  weekend" (job run_daily senza `days=`, assenza di isoweekday/weekday/%7
  nella pipeline, auto_bet run_repeating 3h).

- **auto_bet → execution_engine LIVE (08/09)**: i segnali value/strong_value
  del job 08:50 possono ora piazzare ORDINI REALI su SX Bet (uscita dal
  SIM-only). Si attiva solo con `AUTO_BET_MODE=live|real` E provider reale
  configurato (`EXECUTION_PROVIDER=sxbet` + `SX_API_KEY`/`SX_PRIVATE_KEY`);
  senza, resta SIM (default) o fail-closed con allow_sim=False. Flusso:
  risoluzione del mercato exchange per la STESSA partita (nomi squadre +
  kickoff, univocità — mai ordini su eventi ambigui,
  `execution_engine.resolve_match_market`, provider sxbet), floor EV (si
  riempie SOLO alla quota-segnale o meglio: best < quota → salto), ordine
  IOC con bound = quota del segnale, ledger `bets` con mode='live' +
  market_id/selection_id/bet_id reali e stake/prezzo MATCHED. Gli ordini
  rifiutati o i salti non lasciano righe (un FAILED verrebbe saldato come
  perdita). Risk caps invariati (correlation 30% + esposizione 40%).
- **Staking 100% dinamico (08/09)**: rimosse le regole fisse (era minimo
  2.00 EUR / step 0.50). Il Kelly frazionato (0.05-0.40, env
  `KELLY_MIN_FRACTION`/`KELLY_MAX_FRACTION`) calcola lo stake per OGNI
  scommessa sul bankroll corrente: in LIVE il bankroll è il SALDO REALE
  del proxy wallet SX (`get_balance` → `availableBalance`), non la cassa
  simulata (se il saldo è < minimo ordine → nessuna puntata, fail-closed).
  Floor = minimo ordine exchange (`STAKE_MIN_EUR`, default 1.0 = 1 USDC),
  step 0.01 (`STAKE_STEP_EUR`), cap per singola bet `STAKE_CAP_PCT` 10%
  (value) / `STAKE_CAP_PCT_STRONG` 25% (strong_value): esposizione solo
  sui segnali a forte margine. Kelly riduce naturalmente lo stake sulle
  quote ad alta varianza (formula piena). `/autobet now` esegue il giro
  SUBITO da Telegram (senza attendere il job 08:50).
- **Auto-bet 24/7 (08/09)**: il giro puntate gira OGNI 3h (da 08:50 ITA,
  `run_repeating` in bot.py) invece di una sola volta al giorno: i nuovi
  segnali value delle analisi (04:00/12:00/18:00 UTC, finestra candidati
  mobile 24h) vengono scommessi entro 3h, giorno e notte. Sicurezza:
  UNIQUE(match_id, esito) evita doppioni, guardia 15 min evita ordini a
  partita iniziata, e il cap esposizione TOTALE è GIORNALIERO e
  multi-giro (`auto_bet._today_placed_stake` sottrae l'esposizione già
  piazzata nei giri precedenti dentro `apply_total_exposure_cap`): con
  più giri al giorno il 40% di bankroll vale sul giorno intero, non per
  singolo giro.
- **Fix loop infinito football_hist (08/09)**: il ramo "retry" di
  `sync_history` faceva `continue` senza limite sulla stessa stagione
  (osservato in produzione: "Retry stesso anno per Serie A 2024" a ~50
  righe/sec per oltre 25 min, bloccando il job e bruciando la quota
  API-Football). Aggiunto `MAX_YEAR_RETRIES` (3) con sleep tra i tentativi:
  dopo il limite la stagione è trattata come non accessibile e si passa
  all'anno precedente (mai più blocchi). Test dedicati.
- **Kill-switch Telegram `/autobet` (08/09)**: comando admin che blocca
  le puntate automatiche da remoto in emergenza. Override PERSISTENTE in
  `data/execution/auto_bet_mode.json` (volume condiviso, sopravvive ai
  redeploy) con precedenza su `AUTO_BET_MODE` env: `/autobet off|stop` =
  STOP TOTALE (nessuna puntata, né reale né simulata), `/autobet sim|pause`
  = pausa ordini reali (resta paper trading), `/autobet live|resume` =
  ripristina l'env, `/autobet` = stato (effective/override/env/provider).
  Se il giro 08:50 viene saltato per kill-switch OFF, il job notifica
  l'admin. Test dedicati in test_auto_bet_killswitch.py.
- **Check proxy wallet SX (08/09, live su Railway)**: `execution_engine.py
  --balance` (nuova flag CLI) legge `user/balance-v3`: proxy wallet
  `0x97aE...44002` deployato e finanziato, **12.28 USDC disponibili**,
  exposure 0, nessun ordine aperto (la prova Nueva Chicago saldata).
  ⚠️ Saldo basso: con 2-3 segnali value da €2-5 il wallet potrebbe non
  coprire tutti gli stake (ordini in eccesso falliscono in modo
  fail-closed, nessuna riga sul ledger) — valutare un top-up prima di
  affidarsi all'automazione 24/7.
- **Correzioni ML post-diagnostica (06/09)** — il report sul backtest
  storico (12.909 partite) mostrava: ROI -2,52% flat, controllo a quota
  CLOSING -3,03% (la selezione NON batte il mercato devigato),
  overconfidence crescente sui bucket alti (0.5-0.6: hit 49% vs 55 atteso;
  0.6-0.7: 56% vs 65; 0.7-1.0: 67% vs 85), CLV vig-free negativo ovunque
  e leak sistematico sull'OU2.5 (-6,8% su 924 bet, under -7,3%). Tre
  azioni eseguite (commit in corso):
  1) **OU2.5 ESCLUSO DEFINITIVAMENTE dalle selezioni (06/09)** — `fixture_engine`
     non genera piu' candidati Over/Under, SENZA escape hatch (rimosso
     `ENABLE_OU_MARKET`; `OU_ENABLED` è costante False). Il sistema elabora
     SOLO segnali 1X2. Il ledger previsioni smette di imparare dal mercato
     perdente.
  2) **Ensemble ML ATTIVATO in produzione** — prima non esisteva
     `data/ensemble_model.json` sul volume Railway: `get_ensemble()`
     tornava non addestrato e le analisi usavano solo Poisson+blend.
     Aggiunto `ml_ensemble.reset_ensemble_cache()` + job schedulato
     `retrain_ensemble_job` (bot.py, 05:45 UTC): addestra dal ledger
     live (`build_training_rows`, zero API) e salva il modello sul
     volume se il dataset supera MIN_SAMPLES=30 (oggi ~40 righe chiuse:
     il file viene creato al primo giro / primo retrain manuale).
  3) **Shrink sui bucket alti (esperimento, NON in produzione)** — in
     `historical_backtest` (harness di ricerca, flag
     `--high-prob-threshold/--high-prob-shrink`): sopra soglia 0.55 la
     deviazione dal mercato viene compressa del fattore 0.85.
     **Esito misurato (catena di 4 run flat €20 comparabili, 06/09):**
     - baseline OU ON: n=6274, flat -7,99%, closing **-8,32%**;
     - shrink .85 OU ON: n=5297, flat -6,17%, closing -6,62% (aiuta
       col mercato OU attivo: taglia le pick OU marginali, il leak);
     - baseline NO-OU (**config produzione dopo Punto 3**): n=1262,
       flat -6,20%, closing **-6,11%**;
     - shrink .85 NO-OU: n=1305, flat -9,28%, closing **-9,31%**
       (**PEGGIORA**).
     Perche': nella config 1X2-only i bucket alti (0.6+) contengono
     appena 7-9 bet — lo shrink non ha dove agire e comprimendo i
     favoriti ("1") sposta la selezione verso i pareggi/trasferte
     sovraconfidenti (bucket 0.3-0.4: 680 bet, hit 29.4% vs 35 atteso).
     → `value_filter` NON ha ricevuto lo shrink alto. Il gap residuo
     della config 1X2-only e' il bucket BASSO (X/2), non l'alto.
  4) **PATCH CALIBRAZIONE bucket bassi (06/09, IN PRODUZIONE)** — in
     `value_filter.adjusted_probability`: sotto LOW_PROB_THRESHOLD (0.40)
     la deviazione dal mercato viene compressa del fattore LOW_PROB_SHRINK
     (0.85). Misurata sul backtest (run flat €20 NO-OU): closing
     **-6.11% -> -3.08%**, flat -6.20 -> -3.28, n_bets 1262 -> 694
     (le pick X/2 marginali escono dal filtro EV), strong_value
     -0.3% -> **+6.0%**, pocket X +2.6% -> +14.4%, "2" trasferta
     -21.9% -> +22.1% (n=54, campione piccolo ma direzione coerente).
     Nota: il vecchio report "closing -3,03%" del 05/09 era su codice
     precedente (pre-0c9c4bb) e NON e' confrontabile; il baseline
     confrontabile della config produzione e' il run flat NO-OU.
- **Audit crediti the-odds-api + PROFILO SETTEMBRE SOTTO-BUDGET (05/09)**:
  misura live del 05/09 ~16:10 UTC → **239/500 usati, ~260 residui** per
  ~25 giorni (~10,3/giorno sostenibili). Il vincolo originale (~460/mese)
  conteggiava SOLO la rotazione quote: i costi `fetch_scores` del
  settlement (~12-16/giorno) e del surebet NON erano mai stati conteggiati.
  Tagli applicati per arrivare a fine mese SENZA interrompere il bot:
  1) settlement mirato (`get_leagues_with_open_rows`, solo leghe con
     scommesse attive/chiuse da <48h su partite iniziate) + watchdog da 2h
     a 4h (commit 6603920);
  2) rotazione value ridotta a ~3,5/giorno: SOLO i top campionati a 3gg
     (Serie A, PL, La Liga, Bundesliga, Ligue 1, Eredivisie, Serie B, EFL
     Champ) + coppe europee/mercati maggiori a 7gg (CL, EL, MLS,
     Brasileirao, Liga MX, Saudi); TUTTO il resto a 30gg (dormiente fino a
     ottobre, cache del 1° settembre ancora fresca);
  3) surebet: solo MLB a settembre (in stagione, partite ogni giorno =
     campo-test continuo) con TTL 6h invariato; NBA off-season riattivata
     il 1° ottobre.
  Budget atteso: settlement ~2-3 + surebet ~4 + value ~3,5 ≈ **10-10,5
  crediti/giorno** → copre settembre. ⚠️ **RIPRISTINARE il 1° ottobre**:
  tabella SPORTS_INTERVAL_DAYS completa (git log) + `SUREBET_SPORTS`
  `"baseball_mlb"` → `"basketball_nba,baseball_mlb"` (reset crediti + stagione NBA).
- **Calibrazione isotonica dell'ensemble** (04/09, `probability_calibration.py`):
  PAVA numpy-only (zero deps, coerente col progetto) che mappa gli score
  grezzi di XGBoost/LR sulle frequenze EMPIRICHE del dataset storico.
  Fit su split OUT-OF-SAMPLE (30% riservato, seed fisso — mai sui dati di
  training, altrimenti impara l'overfit e non corregge nulla); attiva con
  >=60 campioni chiusi (MIN_CALIB_SAMPLES). Integrata in `ml_ensemble.py`
  (train/predict/save/load + flag `calibrated` nel predict) e metriche
  Brier/ECE pre/post nel report di training (`metrics["calibration"]`).
  Oggi 9 previsioni chiuse → si attiva da sola col ledger che cresce.
- **CLV → staking WIRED** (04/09, auto_bet.py): `has_clv_positive` veniva
  letto ma MAI passato ad `adaptive_stake` (il parametro esisteva da
  sempre) — ora arriva dal CLV rolling di `clv_history`: CLV positivo =
  conferma dell'edge → frazione Kelly piu' alta. Test di wiring dedicati.
- **Cap esposizione totale** (04/09, auto_bet.py): `TOTAL_EXPOSURE_CAP_PCT`
  = 0.40, applicato in FASE 2 DOPO il correlation cap — il portafoglio del
  giorno non supera il 40% del bankroll (varianza additiva tra pick
  indipendenti), scaling proporzionale che preserva il ranking EV.
- **Test motore Poisson/Dixon-Coles** (test_poisson_engine.py): verifica
  della correzione rho (rho<0 → draw piu' alto del Poisson puro),
  coerenza 1X2/OU/BTTS/AH (somma 1), integrazione `expected_goals`.
- **Correlation risk cap** (03/09, auto_bet.py): Kelly assume indipendenza
  tra le puntate ma esiti correlati nello stesso blocco temporale (stessa
  partita 1X2+OU, oppure stessa lega con kickoff entro 90') condividono la
  varianza → `apply_correlation_cap` riduce PROPORZIONALMENTE gli stake
  quando l'esposizione del blocco supera il 30% del bankroll (mantiene il
  ranking EV, non taglia esiti). Gruppamento per LEGA + finestra temporale
  greedy (match_id uguale = sempre stesso blocco). Test dedicati in
  test_auto_bet.py (blocchi disgiunti senza cap, ranking preservato, flusso
  SIM completo).
- **Drift monitor del modello** (03/09, drift_monitor.py): Brier/LogLoss
  ROLLING sulle ultime 30 previsioni chiuse vs baseline storica; alert
  `drift` se il Brier rolling supera 1.30x la baseline o +0.03 in valore
  assoluto → raccomanda il retraining dell'ensemble ML. Sezione 🧠 nel
  report giornaliero (bot.py, fail-safe try/except) + CLI
  `venv/bin/python drift_monitor.py [--json]`. Stato "insufficient" sotto
  le 15 previsioni chiuse (oggi ~8: si attiva da solo col ledger che cresce).
  Migrazione idempotente `settled_at` su predictions/bets in tracker.py
  (stessa convenzione della cassa).
- **Sanity check settlement** (03/09): tripwire anti-contraddizione nei tre
  settle (bets/predictions/cassa): gol negativi/non numerici/`result`
  incoerente → la riga NON si chiude (resta aperta + log); verdetto 'won'
  impossibile coi gol (es. esito 2 con vittoria casa) → bloccato. Inoltre
  `settlement_sanity_check()` + `heal_settled_contradictions()` nel job di
  settlement (`_update_results`, watchdog 4h + job serali): se una riga già
  chiusa ha un verdetto che contraddice i gol CORRENTI (caso Machida: bet
  marcata won dopo correzione punteggio), viene riaperta e ri-saldata
  automaticamente, con alert Telegram `🔔 SANITY CHECK SETTLEMENT`.
  `settle_cassa` ora aggancia le coppie squadre con prefissi club loose
  (CA Osasuna=Osasuna) e preferisce il match PIÙ RECENTE della coppia.
- **Fix bankroll reale** (03/09): `bankroll_stats`/`get_peak_bankroll`/
  `_bankroll_stats` usavano `SUM(cassa.amount)` ma la colonna è `importo`
  → bankroll sempre 0/fallback (auto_bet usava €100 fisso). Corretto;
  dashboard e schedina ora mostrano il bankroll reale (default solo se
  cassa vuota).
- **Dashboard webapp estesa** (03/09): nuove sezioni Calibrazione per
  mercato (ROI vs EV atteso), Puntate automatiche, CLV (raw/vig-free/
  vs Pinnacle) e streak — dati reali da `/api/dashboard` (performance_report).
  Layout con evidenziazione della pagina attiva.
- **Report giornaliero**: nuova sezione `📊 Stato` con streak attuale
  (vittorie/perse di fila + max) e bankroll reale con peak/drawdown.
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
- **Settlement watchdog / self-healing** (`bot.py`, ogni 4h dal 05/09, era
  2h): scarica i risultati e salda bet/previsioni/cassa aperte anche fuori
  dai job serali, con notifica verdetti. Copre redeploy che saltano i job,
  cache stantie, API lente. VERIFICATO IN PRODUZIONE (01/09): 3 bet
  pendenti (Birmingham, Wycombe, Tranmere) saldate al primo giro.
- **Refertazione credit-saving (05/09, `_update_results`)**: il settlement
  interroga `fetch_scores` SOLO per le leghe con scommesse ATTIVE
  (predictions/bets con `esito_finale IS NULL`, o chiuse da <48h per la
  finestra di verifica/heal) su partite GIÀ INIZIATE (5 giorni). Prima si
  interrogavano tutte le leghe con un segnale value negli ultimi 3 giorni:
  leghe con sole partite future o righe chiuse da giorni bruciavano crediti
  senza saldare nulla. Zero scommesse attive = zero chiamate per quella
  lega. Helper `tracker.get_leagues_with_open_rows` + test dedicati in
  test_settlement_watchdog.py. Watchdog da 2h a 4h (il referto non serve
  istantaneo).
- **Fix cache punteggi** (`odds_api.fetch_scores`): la cache non serve più
  partite iniziate da >3h con `completed=False` (cache scritta a partita in
  corso = stantia): refresh forzato, fallback cache solo se l'API fallisce
  (mai meno dati di prima). Causa radice delle bet rimaste aperte 24h.
- **Logging sicuro** (`secure_logging.py`, integrato in run_all/web_api/bot):
  filtro su tutti gli handler che maschera token/chiavi/segreti nei messaggi
  di log (raccoglie i valori da `os.environ` al bootstrap) + `httpx` a
  WARNING (prima loggava gli URL Telegram col token in chiaro). Verificato:
  0 occorrenze del token nei log del nuovo deployment.
- **.dockerignore rinforzato**: `secrets/`, `.env*`, `data/`, `*.key`,
  `*.pem`, `.git/` esclusi dall'immagine (il Dockerfile fa `COPY . .` — prima
  i segreti locali sarebbero finiti nell'immagine Docker).
- **Esecuzione via provider (06/09) + Smarkets e SX Bet (07/09)**: il
  tripwire `test_betfair_removed.py` è stato RIMOSSO (deciso dal proprietario)
  e nasce `execution_engine.py`: interfaccia Python unica
  (`ExecutionProvider`) verso BetInAsia BLACK / MollyBet (protocollo
  Betfair-compatible JSON-RPC, SportsAPING/v1.0, credenziali SOLO da env
  `EXECUTION_APP_KEY/USERNAME/PASSWORD`), **Smarkets** (API REST v3
  `https://api.smarkets.com/v3/`, credenziali `SMARKETS_USERNAME/PASSWORD`,
  base `SMARKETS_API_BASE`; login `POST sessions/` → header
  `Authorization: Session-Token`; prezzi ×1e4, quantità in stake ×1e4,
  buy=BACK/sell=LAY, 1X2 = event `football_match` / market `match_odds` /
  contratti Home/Draw/Away) e **SX Bet** (V3 dal 26/08/2026: REST
  `https://api.sx.bet` su SX Rollup/Arbitrum Orbit chainId 4162, testnet
  `SX_API_BASE=https://api.toronto.sx.bet`; letture pubbliche, scritture con
  header `x-sx-api-key` (`SX_API_KEY`), ordini firmati EIP-712 con la chiave
  privata dell'EOA `SX_PRIVATE_KEY` — richiede `eth-account` in
  requirements.txt, import lazy; 1X2 calcio = market type 1 binario "X vs
  Not X" Home/Tie/Away, `percentageOdds` prob ×1e20 ladder 0.125%,
  `totalBetSize` in unità USDC 6 decimali, IOC/FOK = take, GTC = rest;
  ⚠️ i fondi stanno nel proxy wallet dell'account, NON nell'EOA).
  `DryRunProvider` senza
  credenziali e probe a stake minimo (`EXECUTION_MIN_STAKE_EUR`=1€) che
  misura latenza e slippage reali loggandoli in
  `data/execution/measurements.jsonl`; discovery `--markets` per elencare i
  match odds calcio con i selection/contract id. ⚠️ `api.smarkets.com` è
  inibito dall'ADM in Italia (DNS → sito-inibito-giochi.adm.gov.it): il
  probe reale va eseguito da Railway/rete non italiana; `api.sx.bet` invece
  è raggiungibile dall'Italia (verificato 07/09). Health mostra
  `betfair_enabled: false` fisso (compatibilità
  frontend); `/api/scan` resta 503 "betfair_removed" (endpoint rimosso).
- **Refertazione SOLO the-odds-api**: risultati e saldaggio bet/previsioni/
  cassa passano ESCLUSIVAMENTE da `odds_api.fetch_scores` +
  `match_scores_by_name` (la stessa chiave delle quote restituisce i
  risultati FINITI della stagione corrente, aggancio diretto ai match_id
  the-odds-api già in `matches`). TENTATIVO API-Football il 04/09 e
  REVERSO nello stesso giorno: il piano free copre solo le stagioni
  2022-2024 (errore API "Free plans do not have access to this season"),
  quindi NON può saldare le partite correnti del 2026 — verificato con
  chiamata reale su copia del DB di produzione (0 match aggiornati).
  API-Football resta solo per lo storico ratings in `football_hist.py`.
  Vincolo garantito da `test_settlement_source.py` e dai test di
  settlement (patch del confine `odds_api.fetch_scores`).
- **auto_bet SIM-only permanente (04/09)**: il job 08:50 piazza puntate
  SIMULATE con la quota del segnale (mode='sim', niente conto Exchange);
  alimenta ledger/ML/CLV come prima, saldate a fine partita. I candidati
  del giorno usano una FINESTRA MOBILE di 24h (non il giorno calendario
  UTC): a fine giornata un match con kickoff poco dopo la mezzanotte
  cadrebbe nel giorno dopo e verrebbe perso dal filtro per data.
- **Bugfix 02/09**: `auto_bet.run_today_bets` senza guardia `stake <= 0` sul
  percorso a stake fisso (possibile bet da €0.00); `web_api._schedina_json`
  con UnboundLocalError (`_bankroll` locale oscurava la funzione modulo →
  `/api/schedina` rotto con adaptive attivo). Test aggiornati all'era
  adaptive: percorso fallback testato con stub `sys.modules["adaptive_staking"]
  = None` + nuovo test del wiring adaptive (stake Kelly usato davvero).
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
- **Vault segreti**: attivo da locale (vault.bin Fernet/PBKDF2, 6 segreti
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
- **Puntate automatiche** (`auto_bet.py`, job 08:50 ITA): SIM di DEFAULT
  (quota del segnale, mode='sim') oppure **LIVE via execution_engine dal
  08/09** con `AUTO_BET_MODE=live` + provider reale (SX Bet; ordini reali
  con floor EV e risoluzione evento univoca, mode='live' nel ledger).
  Stake **ADATTIVO** (`adaptive_staking.py`):
  Kelly frazionato dinamico (0.10-0.35 vs 0.25 fisso prima) con drawdown
  protection (>10% drawdown → riduzione stakes) e confidence weighting
  (market_edge alto + strong_value → stake più alto). Cap: 3% value, 5%
  strong_value. Fallback: stake fisso `BET_STAKE_EUR` se modulo assente.
  Guardie: salta partite a <15 min dall'inizio, doppie puntate (UNIQUE
  match_id+esito). Risk caps prima del salvataggio: correlation cap (30%
  bankroll per blocco correlato) + cap esposizione totale (40%). Registro
  in tabella `bets`, saldato a fine partita (`settle_bets`) e incluso nel
  riepilogo.
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
- **Dedup dataset ML** (02/09, audit hash 36aa024f): doppio livello —
  (1) `tracker` migra automaticamente i vincoli UNIQUE sui ledger ANCHE per
  DB nati prima (dedup normalizzato: "Over 2.5"=="over", "Inter"=="1" via
  home/away; backup _old mai perso, recupero automatico se la migrazione
  viene interrotta); (2) `ml_dataset.dedupe_training_rows` dedup con chiave
  normalizzata a livello pipeline (stessa scommessa da predictions+bets =
  1 riga) — idempotente per CLI/API/ensemble/audit. `ml_audit` usa la
  STESSA chiave (audit e pipeline concordano). VERIFICATO IN PRODUZIONE:
  autoindex UNIQUE attivi su predictions/bets, 0 duplicati, audit pulito.
- **CLV vig-free corretto** (02/09): `performance_report._clv_stats` ora
  USA davvero `clv_vig_free()` (devig) invece di duplicare il vs-Pinnacle,
  e le righe con UN SOLO campione prezzo (closing = eco del segnale, CLV
  finto 0) sono escluse dalle medie. Il CLV vig-free -3.85% del 01/09 era
  esattamente 1/1.04-1: artefatto del fallback overround stimato, NON un
  segnale di mercato. Il report mostra il conteggio "in attesa di chiusura".
- **Backtest & Monte Carlo** (`backtest_mc.py`, 02/09): walk-forward SENZA
  look-ahead (ensemble addestrato solo sulle giornate precedenti; XGBoost se
  disponibile, altrimenti LR) + staking con l'ADAPTIVE_STAKING di produzione
  + 1000 percorsi Monte Carlo: ROI base/mediana/p5-p95, MAX DRAWDOWN
  base/mediana/p95, P(riduzione), P(≥5 perdite di fila). CLI
  `venv/bin/python backtest_mc.py --formato` e comando `/backtest_mc [sims]`.
  Guardia: servono ≥10 righe chiuse (oggi 8: si attivera' da solo con il
  ledger che cresce — ritornare quando il ledger ha 15+ chiusure).
- **Alert crollo quota** (rlm_alert.py, 02/09): oltre a RLM/steam, nuovo
  trigger URGENTE "CROLLO QUOTA" (calo ≥5% dal primo snapshot, basta 1
  aggiornamento = 2 snapshot per la velocità). Job già attivo ogni 5'
  14:00–23:50 ITA; destinatari: admin + iscritti, cooldown 60'/match.
- **Segnali mercato nel report + webapp** (market_signals.py, 02/09):
  aggregatore condiviso che classifica i segnali value attivi con i VERI
  rilevatori (line_movement + rlm_alert: steam/crollo/RLM, niente più proxy
  euristici). Esposto in: sezione "Line Movement" di `format_daily_report`,
  `GET /api/market_signals` (summary + signals ordinati per severità) e
  pagina webapp `/movimenti` (badge per tipo + card per segnale). CLI
  `venv/bin/python market_signals.py [--json]`. Fix build webapp: rimossa
  chiamata morta `proStake` in schedina/page.tsx (rompeva `npm run build`).
- **Backup centralizzato** (`backup_manager.py`, 02/09): snapshot
  data/backups/<ts>/ con DB (SQLite backup API) + INTEGRITY CHECK
  (PRAGMA quick_check) + dataset ML RIGENERATO (csv+json, sempre fresco e
  già deduplicato) + copia data/. Rotazione BACKUP_KEEP (env, default 7),
  timestamp con microsecondi. Usato da backup_data_job (03:30 UTC + avvio)
  e comando `/backup` (solo admin). VERIFICATO IN PRODUZIONE: integrity ok,
  56 CLV, dataset ML, 78 file data/.
- **Test**: 529 test verdi (la suite completa richiede ~8 min).

## Moduli avanzati (Settembre 2026)

- **ML Ensemble** (`ml_ensemble.py`): Logistic Regression numpy-only che
  combina le probabilità Poisson con un classificatore addestrato sul
  dataset storico. Peso dinamico basato sul Brier score. Save/load in
  `data/ensemble_model.json`. Integrato in `fixture_engine._analyze_match`.
- **Calibrazione isotonica** (`probability_calibration.py`, 04/09): PAVA
  numpy-only che corregge l'overconfidence di XGBoost/LR mappando gli
  score sulle frequenze empiriche. Fit su split out-of-sample (mai sui
  dati di training), attiva con ≥60 campioni chiusi, metriche
  Brier/ECE pre-post nel report. Integrata in `ml_ensemble.py`
  (train/predict/save/load, flag `calibrated`).
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
- **Dedup dataset ML** (02/09, audit hash 36aa024f): doppio livello —
  (1) `tracker` migra automaticamente i vincoli UNIQUE sui ledger ANCHE per
  DB nati prima (dedup normalizzato: "Over 2.5"=="over", "Inter"=="1" via
  home/away; backup _old mai perso, recupero automatico se la migrazione
  viene interrotta); (2) `ml_dataset.dedupe_training_rows` dedup con chiave
  normalizzata a livello pipeline (stessa scommessa da predictions+bets =
  1 riga) — idempotente per CLI/API/ensemble/audit. `ml_audit` usa la
  STESSA chiave (audit e pipeline concordano). VERIFICATO IN PRODUZIONE:
  autoindex UNIQUE attivi su predictions/bets, 0 duplicati, audit pulito.
- **CLV vig-free corretto** (02/09): `performance_report._clv_stats` ora
  USA davvero `clv_vig_free()` (devig) invece di duplicare il vs-Pinnacle,
  e le righe con UN SOLO campione prezzo (closing = eco del segnale, CLV
  finto 0) sono escluse dalle medie. Il CLV vig-free -3.85% del 01/09 era
  esattamente 1/1.04-1: artefatto del fallback overround stimato, NON un
  segnale di mercato. Il report mostra il conteggio "in attesa di chiusura".
- **Backtest & Monte Carlo** (`backtest_mc.py`, 02/09): walk-forward SENZA
  look-ahead (ensemble addestrato solo sulle giornate precedenti; XGBoost se
  disponibile, altrimenti LR) + staking con l'ADAPTIVE_STAKING di produzione
  + 1000 percorsi Monte Carlo: ROI base/mediana/p5-p95, MAX DRAWDOWN
  base/mediana/p95, P(riduzione), P(≥5 perdite di fila). CLI
  `venv/bin/python backtest_mc.py --formato` e comando `/backtest_mc [sims]`.
  Guardia: servono ≥10 righe chiuse (oggi 8: si attivera' da solo con il
  ledger che cresce — ritornare quando il ledger ha 15+ chiusure).
- **Alert crollo quota** (rlm_alert.py, 02/09): oltre a RLM/steam, nuovo
  trigger URGENTE "CROLLO QUOTA" (calo ≥5% dal primo snapshot, basta 1
  aggiornamento = 2 snapshot per la velocità). Job già attivo ogni 5'
  14:00–23:50 ITA; destinatari: admin + iscritti, cooldown 60'/match.
- **Segnali mercato nel report + webapp** (market_signals.py, 02/09):
  aggregatore condiviso che classifica i segnali value attivi con i VERI
  rilevatori (line_movement + rlm_alert: steam/crollo/RLM, niente più proxy
  euristici). Esposto in: sezione "Line Movement" di `format_daily_report`,
  `GET /api/market_signals` (summary + signals ordinati per severità) e
  pagina webapp `/movimenti` (badge per tipo + card per segnale). CLI
  `venv/bin/python market_signals.py [--json]`. Fix build webapp: rimossa
  chiamata morta `proStake` in schedina/page.tsx (rompeva `npm run build`).
- **Backup centralizzato** (`backup_manager.py`, 02/09): snapshot
  data/backups/<ts>/ con DB (SQLite backup API) + INTEGRITY CHECK
  (PRAGMA quick_check) + dataset ML RIGENERATO (csv+json, sempre fresco e
  già deduplicato) + copia data/. Rotazione BACKUP_KEEP (env, default 7),
  timestamp con microsecondi. Usato da backup_data_job (03:30 UTC + avvio)
  e comando `/backup` (solo admin). VERIFICATO IN PRODUZIONE: integrity ok,
  56 CLV, dataset ML, 78 file data/.
- **Surebet engine indipendente** (`surebet_engine.py`, 05/09): scanner di
  arbitraggio su mercati h2h a 2 esiti per NBA (`basketball_nba`), MLB
  (`baseball_mlb`) e Tennis (chiavi per torneo `tennis_*`, configurabili via
  `SUREBET_SPORTS`). Default CREDITO-CONSERVATIVO: solo NBA+MLB con TTL 6h
  (~8 crediti/giorno, ~240/mese — la chiave e' CONDIVISA col calendario
  value che ne usa ~407-460 su 500 del piano free: NON aggiungere tornei
  tennis se i crediti residui sono bassi). Trigger matematico (1/A)+(1/B)<1 su coppie di bookmaker
  con ALMENO un SOFT (`SUREBET_SOFT_BOOKS`: Snai, GoldBet, Bet365, William
  Hill, Bwin, Unibet, Sisal, Eurobet, Betflag, Novibet, Stanleybet, 888,
  Marathonbet, 10bet, Betway, Paddy Power, Coral, betsson) contro SHARP
  (`SUREBET_SHARP_BOOKS`, default Pinnacle) o soft-vs-soft. Stake esatti
  proporzionali agli inversi su `SUREBET_BUDGET` (default €100), profit/ROI
  netto garantito. **INDIPENDENZA totale dal bot Value Bet**: cache propria
  (data/surebet/cache), log JSONL proprio (data/surebet/opportunities.jsonl,
  dedup 24h), nessun import da tracker/bot (test dedicato lo verifica),
  loop separato `venv/bin/python surebet_engine.py --loop N` (non toccare
  run_all.py: il bot resta sul volume unico). Delivery: Telegram con formato
  dedicato (ROI, evento, quote, stake per bookmaker) via POST diretto
  all'API Telegram + webhook n8n già predisposto (`SUREBET_WEBHOOK_URL`,
  payload JSON via `build_json_payload`). Crediti: TTL 1h per sport, stop
  sotto `SUREBET_MIN_REMAINING` (default 50; il piano free ~500/mese è già
  quasi tutto consumato dal calendario value → monitorare crediti).
  Tripwire: nessun riferimento al vecchio exchange nel codice del modulo.
  **COLLAUDO REALE 05/09 su Railway** (chiave API vera): scansione NBA → 41
  match, 39 con h2h a 2 esiti, **3 surebet reali trovate** (es. Rockets vs
  Mavericks: Dallas @4.25 1xBet vs Houston @1.34 Nordic Bet, ROI +1.88%).
  NESSUNA Pinnacle/sharp per NBA in eu/uk (tutte le coppie sono soft-soft):
  la copertura sharp dipende dallo sport — verificare per MLB/tennis.
  Fix delivery: parse_mode HTML (Markdown legacy dava 400 su nomi reali).
  Fix anti-garbage 05/09: filtro `SUREBET_MAX_ODDS` (default 30) PRIMA del
  trigger matematico — coppie con quote sporche/mercati illiquidi (es. reale
  Arizona @85.00 vs @1.03: (1/85+1/1.03)<1 scatta per artefatto aritmetico)
  scartate da `is_sane_odds()`. Test dedicati (TestMaxOddsFilter).
  **VERIFICA CRON IN PRODUZIONE (05/09)**: il cron */15 gira davvero. I log
  delle esecuzioni dei cron job Railway NON sono esposti dal CLI (si vede
  solo "Mounting volume/Starting Container") → il "0 log" era un falso
  allarme. Cattura live via `railway ssh -s surebet` durante un run (con
  hold temporaneo): heartbeat.json scritto da pid 1, cache MLB aggiornata
  (remaining 263, 15 match) e 69 opportunita' gia' loggate sul volume.
  Aggiunti: heartbeat.json a ogni run (ts/sports/pid), log INFO
  avvio/completamento, hold opzionale `SUREBET_CRON_HOLD_SECONDS` (default
  0, usato solo per diagnosi) per tenere il container attivo qualche
  secondo ed ssh-are durante il run. Config attiva 05/09: MLB-only (NBA
  off-season), TTL 6h, cron */15.
- **Test**: 547+ test verdi (la suite completa richiede ~9 min).
- **Sicurezza**: rotazioni token 01/09, 02/09 e **04/09** verificate
  (Telegram `@Calcifrrbot`, ID 8372645521). Rotazione 04/09 completata
  con Opzione A: token esposto in chat REVOCATO (getMe col vecchio → 401)
  e nuovo token attivo (getMe 200), letto dalle env Railway e portato nel
  vault cifrato locale SENZA mai apparire in chat (46 chars, diff vs
  vecchio verificata). Tripwire `test_secret_hygiene.py` rende permanente
  il vincolo "nessuna credenziale in chiaro nel codice" (vedi regola 7).

## Prossimi passi possibili (non urgenti)

- ExecutionEngine: ottenere le credenziali (SX Bet: `SX_API_KEY`/`SX_PRIVATE_KEY`
  + proxy wallet deployato e finanziato su SX Rollup; Smarkets:
  `SMARKETS_USERNAME`/`SMARKETS_PASSWORD`; aggregatore BetInAsia BLACK /
  MollyBet: `EXECUTION_APP_KEY/USERNAME/PASSWORD`), caricarle nelle env
  Railway + vault locale e fare le prime chiamate reali:
  `venv/bin/python execution_engine.py --provider sxbet --probe
  --market <marketHash_hex> --selection 1` (o `--provider smarkets`, o senza
  `--provider` per l'aggregatore) con stake 1€ per misurare latenza/slippage
  veri prima di passare a stake reali. ⚠️ Da rete italiana `api.smarkets.com`
  è inibito (ADM): il probe Smarkets va eseguito da Railway/rete estera;
  `api.sx.bet` è raggiungibile. Wiring auto_bet → execution_engine
  completato l'08/09 (vedi Stato attuale): resta il collaudo live in
  produzione (AUTO_BET_MODE=live + EXECUTION_PROVIDER=sxbet su Railway).
- Surebet engine: schedulare il loop in produzione (crontab/cron Railway o
  secondo servizio) e monitorare i crediti the-odds-api (il piano free è
  quasi saturo col calendario value). Verificare su dati reali quali
  bookmaker soft the-odds-api copre davvero per NBA/MLB/Tennis (il match
  per sottostringa è estensibile via env).
- Quando il ledger avrà 100+ previsioni chiuse: usare `market_diagnose.py`
  per identificare mercati critici e ajustare blend/devig/soglie.
- Eseguire `/backtest_mc` con 15+ previsioni chiuse (oggi 8): le metriche
  Monte Carlo (MaxDD p95) diventano significative solo con abbastanza dati.
✅ Segnali RLM/steam/crollo nel report + webapp (02/09, market_signals.py).
- Integrazione XGBoost quando il dataset ML raggiunge 500+ campioni
  (attualmente Logistic Regression numpy-only per evitare deps pesanti).
- Cambiare `IT_OFFSET` da 2 a 1 a fine ottobre (ora legale invernale).
