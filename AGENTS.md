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
team_names.py       → risoluzione nomi squadra bookmaker→DB (11/09: 'Tottenham
                      Hotspur'→'Tottenham', 'Wrexham'→'Wrexham AFC', ...)
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
tennis_sandbox.py   → SANDBOX tennis (08/09, ELO esteso il 09/09): paper
                      trading Moneyline 2 vie su SX Bet (type 52), baseline
                      ELO superficie-specifico (cemento/terra/erba) con
                      time-decay 30/60gg seminata dal mercato, +EV con
                      anti-spurio (inv_sum 0.98-1.08), ledger SQLite dedicato
                      (data/tennis_sandbox/), ZERO ordini reali e zero crediti
                      the-odds-api (letture SX pubbliche)
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

## Stato attuale (aggiornato al 09/09/2026)

- **FIX regressione 71b2c4c (09/09, deployato c299abe)**: il cleanup OU2.5
  aveva rimosso il parametro posizionale `p_over` dalla chiamata a
  `tracker.save_analysis` in `fixture_engine._analyze_match` senza
  aggiornare la firma → ogni analisi partita crashava con TypeError
  ("missing 1 required positional argument: 'status'"), abortendo l'intera
  lega nel giro calendario (il prossimo giro 10/09 04:00 UTC sarebbe
  fallito). Ripristinato `p_over=None` (colonna legacy `prob_over` del
  ledger). Stessa regressione su bot.py: `format_segnale_pronto` non ha
  piu' `quota_over` ma `cmd_test_segnale`/`cmd_segnale` passavano ancora 7
  argomenti posizionali → `/segnale` e `/test_segnale` rispondevano
  "Errore nel calcolo. Riprova.". Ora i chiamanti usano keyword e
  `cmd_segnale` ha perso il lookup legacy delle quote Over (bookmaker
  sempre "Modello" con caveat). Ripristinata la costante `OU_ENABLED =
  False` in fixture_engine (documentata qui sotto, tripwire
  `test_ou_exclusion`); test aggiornati (test_bot: `test_escluso_over_under`
  asserisce l'ASSENZA di OU). Verificato: suite completa 831 test verdi,
  smoke test su produzione OK. NB: le analisi girano SOLO quando una lega
  e' "dovuta" (rotazione 3gg top leghe): dopo il 07/09 04:00 il prossimo
  giro con refresh e' il 10/09 04:00 — giorni senza analisi sono attesi
  per design.
- **Stagger rotazione quote (10/09)**: con le leghe core tutte a 3gg e
  cache sincronizzate, le analisi avvenivano solo 1 giorno su 3 (gli
  altri giorni `due` era vuoto → 0 match analizzati → auto-bet senza
  candidati freschi). `odds_api.is_sport_due` ora applica una FASE stabile
  per lega (hash → 0..intervallo-1): sul "giorno di fase" (cache con eta'
  >= 1gg) la lega core (intervallo <= 7gg) diventa dovuta anche prima
  della scadenza, spalmando le scadenze su giorni diversi → analisi
  GIORNALIERE a costo invariato (ogni lega resta sul suo intervallo; le
  leghe 30gg restano dormienti pure, zero costi extra). Test dedicati in
  test_odds_api.py (fasi distinte, scadenza su giorno di fase, 30gg non
  anticipate, intervallo mai allungato).
- **PRIMA SCOMMESSA LIVE REALE piazzata (09/09 18:36 UTC) + fix dei 3
  bug che bloccavano auto_bet (commit `ad50810`)** — il giro automatico
  (ogni 60s) ha piazzato la bet #18: **Charlton Athletic (1) @ 3.3333,
  1 USDC, FULLY_FILLED, bet_id reale SX** (prezzo MIGLIORE del segnale
  3.25: floor EV rispettato), wallet 47.16 → 46.16 USDC. Prima del fix
  il bot loggava "0 puntate" da giorni NONOSTANTE segnali value attivi
  nel ledger. Bug fixati:
  1) **`_today_value_picks` leggeva `match_analysis`** (solo best-per-EV,
     che può essere `rejected`) invece del ledger `predictions` (status
     per OGNI esito): Derby (best=Draw EV 0.151 rejected, ma Derby/West
     Brom value nel ledger) → 0 candidati → il bot non piazzava mai.
     Ora JOIN su `predictions` (mercato='1X2', status value/strong_value,
     esito_finale IS NULL), un pick per match = best EV tra i value.
  2) **`"over" in el` sul settlement** (`_prediction_outcome`/
     `_esito_won`/`_esito_possible`): 'Blackburn **Rovers**' contiene
     'over' come sottostringa → la pred 1X2 #98 veniva saldata come
     Over 2.5 (won coi gol 1-2) e bloccata dal sanity check per sempre.
     Ora il match OU scatta SOLO se l'esito inizia con 'over'/'under'
     come parola intera. Pred #98 risaldata: Blackburn `lost` -1.0.
  3) **TEAM_MAP senza alias** per Derby County/West Bromwich Albion/
     Cardiff City/FSV Mainz 05/Swansea City/Atalanta BC/AS Roma →
     `_canonical_esito` non risolveva quegli esiti. Alias aggiunti.
  Verificato live: `_prediction_outcome` Blackburn='lost'; `_today_value_
  picks` ora trova i candidati (Charlton, West Brom). Prossimi candidati
  in finestra: Union Berlin (11/09) e i 20+ segnali del 12/09 — il bot
  li piazzerà automaticamente appena entrano nella finestra mobile 24h.
- **AUTO-BET LIVE minuto-per-minuto + staking prudente (09/09)** —
  configurazione operativa richiesta dal proprietario: 1) **Frequenza**:
  `auto_bet_job` passa da ogni 3h a **ogni 60s** (`run_repeating`
  interval=60, first=60, `max_instances=1` anti-sovrapposizione) — il
  giro non brucia crediti the-odds-api (segnali dal DB + prezzi SX
  dall'API pubblica) e il floor EV cattura i miglioramenti di prezzo fino
  alla guardia 15 min pre-kickoff. 2) **Kelly FISSATO al 5%**: env
  `KELLY_MIN_FRACTION=KELLY_MAX_FRACTION=0.05` (la frazione dinamica
  0.05-0.40 resta nel codice, con MIN=MAX è sempre 0.05). 3) **Cap 2%**
  per singola operazione: `STAKE_CAP_PCT=0.02` e
  `STAKE_CAP_PCT_STRONG=0.02`. ⚠️ Il floor exchange 1 USDC prevale sul
  cap: con wallet ~47 USDC lo stake effettivo è 1 USDC (>2%); il cap 2%
  diventa vincolante da ~50 USDC in su (top-up consigliato). 4) Soglie
  EV invariate e già attive (+3% value, strong_value EV>8% + edge ≥+5pp)
  e drift watchdog ogni 6h invariato. Anti-spam: l'avviso kill-switch
  OFF (che col giro ogni minuto scattava 1440 volte/giorno) è limitato a
  **1 alert/giorno** (chiave `KS_OFF` su tracker.is_notified). Le env
  sopra sono DICHIARATE in `.railway/railway.ts` (preserve) insieme a
  AUTO_BET_MODE/EXECUTION_PROVIDER/SX_* per non farle distruggere da
  `railway config apply`; su Railway sono già impostate (l'esecuzione
  LIVE era già attiva). Startup invariato: `Dockerfile` → `run_all.py`
  (processo bloccante, i job vivono nella job_queue del bot).
  Test aggiornati: `test_football_scan_h24.py` ora impone interval=60.
- **ELO tennis superficie-specifico + time-decay nel sandbox (09/09)** —
  esteso il modello del `tennis_sandbox` prima di affidargli piu' superfici:
  1) **Surface-Specific ELO**: ogni giocatore ha un rating OVERALL + rating
  per superficie (hard=cemento/clay=terra/grass=erba). La superficie del
  torneo e' rilevata dai campi testo del mercato SX (`market_surface` →
  `detect_surface`, matching pesato su keyword di tornei; NIENTE `surface`
  in V3). Se il torneo non e' riconosciuto o il testo e' ambiguo → None
  (mai indovinare: una superficie sbagliata contaminerebbe il rating).
  Un match su superficie nota aggiorna overall + superficie; uno
  sconosciuto SOLO l'overall. Ledger migrato (ALTER idempotente): colonna
  `surface` su `signals` e `observations`. 2) **Time-decay 30/60 giorni**: i
  risultati invecchiano all'USO — la rating efficace regredisce verso il
  neutro 1500 in base all'eta' dell'ultimo match osservato: peso PIENO
  sotto `ELO_RECENT_DAYS` (30), ~50% a ~60gg (`ELO_HALF_LIFE_DAYS` 30),
  zero oltre `ELO_WINDOW_DAYS` (365). Il seeding dal mercato (n=0) non
  invecchia. 3) La probabilita' per i +EV usa la **blended rating**: con
  storico su quella superficie domina la superficie (peso
  `min(1, n_surf/SURFACE_MIN_MATCHES)`), senza storico l'overall. Il
  settlement passa la superficie dell'observation a `elo.update`, quindi le
  superfici imparano solo dai match su quella superficie. `ratings.json`
  RETROCOMPATIBILE (vecchio formato → overall, superfici vuote). Report
  con riepilogo per superficie (`🏟️ Per superficie`). Test dedicati
  (decay 30/60/365gg, isolamento superfici, blend, migrazione ledger,
  retrocompatibilita', rilevamento tornei).
- **Contenimento staking tennis sandbox (09/09)**: ridotti i default di
  staking per evitare puntate eccessive (osservata una stake paper 50 su
  Alcaraz) finche' l'EV non si stabilizza: `TENNIS_KELLY_FRACTION`
  0.25 -> **0.10**, `TENNIS_MAX_STAKE_PCT` 0.05 -> **0.02** (2% del
  bankroll virtuale) e NUOVO tetto assoluto `TENNIS_MAX_STAKE_ABS`
  (default **25**) in `kelly_stake` (min tra Kelly frazionato, cap %% e
  cap assoluto). Con bankroll 1000 lo stake paper massimo passa da 50 a
  20. Nessun override env su Railway: i nuovi default valgono dal deploy.
  Test aggiornati (TestKelly: 20 con nuovi default + tetto assoluto 25
  che prevale sul cap %% alto).
- **Calibrazione isotonica ATTIVA in produzione (09/09)**: soglia
  `MIN_CALIB_SAMPLES` abbassata da 60 a **50** in
  `probability_calibration.py` (il ledger era fermo a 57 chiusure: con
  30% OOF servivano comunque 17 punti di calibrazione). In piu' il retrain
  ora gira anche al BOOT (`run_once(retrain_ensemble_job, when=20)` in
  bot.py): al primo deploy che introduce la nuova soglia la calibrazione
  si attiva SUBITO, senza attendere le 05:45 UTC del giorno dopo (il job
  e' idempotente, zero API). Verificato in simulazione: 57 campioni ->
  calibrator fitted (n_cal=17, pre_brier 0.3163 -> post_brier 0.2412,
  ECE 0.3152 -> 0.0).
- **Drift watchdog in background (09/09, bot.py)**: nuovo job
  `drift_watchdog_job` ogni 6h (first=1800) che controlla la calibrazione
  rolling e allerta admin+iscritti SOLO su status="drift" (anti-spam:
  transizione a drift oppure 1 alert/24h se persiste; stato sempre
  loggato). Il retraining e' gia' coperto da 05:45 UTC + boot: l'alert e'
  il campanello, non l'azione. Nuovo endpoint **GET /api/drift** in
  web_api.py per la verifica REMOTA del drift (stesso check del job,
  comodo per cron/uptime esterni).
- **Dashboard calibrazione (09/09, webapp + API)**: nuovo endpoint
  **GET /api/calibration** in web_api.py che aggrega l'istantanea
  completa per la nuova pagina webapp `/calibrazione` (link nel menu):
  stato drift (stesso check di /api/drift), metriche training dell'
  ensemble (modello, acc, Brier, peso ML), stato calibrazione isotonica
  (pre/post Brier/ECE, n_cal, curva score->prob calibrata dal
  calibratore) e reliability diagram calcolato sulle previsioni chiuse
  (confidenza vs frequenza empirica, bin a larghezza uguale come l'ECE).
  La pagina disegna le curve in SVG puro (zero dipendenze chart: la
  webapp ha solo next/react) con fallback demo se il backend non e'
  raggiungibile. Test dedicati in test_web_api.py (TestCalibration +
  rotte registrate).
  **Grafico temporale del drift (09/09)**: nuova
  `drift_monitor.brier_history()` — serie walk-forward del Brier/LogLoss
  rolling calcolata a ogni chiusura (finestra 30, min 15), downsampling
  che mantiene SEMPRE l'ultimo punto (il valore corrente); esposta in
  /api/calibration come `drift_history` e disegnata nella pagina con
  baseline e soglia drift (1.30x baseline). Test: TestBrierHistory in
  test_drift_monitor.py. NB: `n` nei punti e' la dimensione della
  finestra rolling (cap 30), non il conteggio cumulato.
- **Retraining ensemble ML su produzione (09/09)**: eseguito a mano sul
  container Railway (`python3 ml_ensemble.py --retrain`) dopo drift
  rilevato dal monitor (Brier rolling 0.2432 vs baseline 0.2073, LogLoss
  0.6789 vs 0.6049, 49 previsioni chiuse): Brier di training 0.2403 ->
  **0.0354**, acc 0.982, n=57 righe, XGBoost, `ensemble_weight` 0.51,
  salvato su /app/data/ensemble_model.json. Calibrazione isotonica
  ancora `skipped` (57 < 60 MIN_CALIB_SAMPLES: si attiva da sola col
  ledger che cresce). NB: il Brier di training NON e' il Brier rolling
  del drift monitor (quello misura le ultime 30 chiusure fatte dal
  modello VECCHIO e resta valido finche' non si chiudono nuove
  previsioni col modello nuovo).
- **Collaudo LIVE calcio verificato (09/09)** — su Railway il sistema era
  GIÀ in esecuzione live effettiva: env `AUTO_BET_MODE=live` +
  `EXECUTION_PROVIDER=sxbet` (credenziali SX presenti), file kill-switch
  `data/execution/auto_bet_mode.json` ASSENTE → a runtime
  `kill_switch_status()` = {override: null, env_mode: live, effective:
  live, provider_ready: true}. Il giro `auto_bet_job` delle 16:42 UTC ha
  loggato "bankroll LIVE = saldo wallet 12.28 USDC" e 0 ordini: NON c'e'
  alcun gate a ~100 campioni che blocchi il live (nel percorso auto_bet
  non esistono soglie di campioni: ensemble 30 / calibrazione 60 / drift 15
  sono solo telemetria) — i 0 ordini dipendono dal filtro value: nelle 24h
  c'erano solo 7 match analizzati, 6 `rejected` e 1 `value` debole
  (Cardiff–Stoke @3.25, kickoff 18:45 UTC, perso dalla guardia dei 15').
  Le puntate sim piu' recenti nel DB restano quelle del 05/09 (Over 2.5,
  pre-esclusione OU). Prossimo giro ogni 3h (es. 19:42 UTC) dopo le
  analisi 18:00 UTC.
- **TRANSIZIONE LIVE UFFICIALE micro-staking calcio 1X2 (09/09)** —
  direttive operative confermate con verifica sul container:
  1) LIVE: env `AUTO_BET_MODE=live` + `EXECUTION_PROVIDER=sxbet` +
     credenziali SX, kill-switch ASSENTE, `kill_switch_status()` =
     {effective: live, provider_ready: true}, `ExecutionEngine()` =
     `SxBetProvider` REALE (mai DryRun), **saldo 47.16 USDC** disponibili
     (proxy 0x97aE…44002, exposure 0), staking `AUTO_BET_STAKE_MODE=
     adaptive` (env flat RIMOSSA, `EXECUTION_DRY_RUN` assente). Zero
     ordini reali finora: NON per configurazione (tutto live) ma perche'
     nessun segnale +EV ha superato i gate dal passaggio a live — il
     primo ordine arrivera' al primo segnale value che passa il filtro
     (es. candidato odierno Watford–Stoke 1 @2.32 EV +15%). Le perdite
     reali del ledger (mode='live') alimenteranno da subito retraining
     (05:45 UTC + boot) e ML: feedback strutturale, niente conteggi fissi
     di giocate/giorno (solo segnali +EV + risk cap 40%/30%).
  2) TENNIS sandbox: limiti ridotti CONFERMATI nel container — env
     `TENNIS_KELLY_FRACTION`/`TENNIS_MAX_STAKE_PCT`/`TENNIS_MAX_STAKE_ABS`
     ASSENTI → valgono i default 0.10 / 0.02 / 25 (stake paper max 20 su
     bankroll 1000); `TENNIS_SANDBOX_ENABLED=1`.
  3) MONITORAGGIO invariato: calibrazione isotonica fitted (Brier OOF
     0.2322→0.1479, ECE→0), drift watchdog ogni 6h, retrain 05:45 UTC +
     boot, `GET /api/drift` + `/api/calibration` attivi.

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
- **Audit crediti 09/09 + fix H2H-only (dimezza il costo quote)**: timeline
  remaining reale: 09/01 13:05 → 488 | 09/03 → 372 | 09/04 → 310 |
  09/07 04:00 → 191 | 09/09 16:47 → **127** (~25/giorno tra 07/09 e 09/09,
  sopraelevato dal doppio addebito `markets=h2h,totals`). **Causa radice
  trovata**: the-odds-api addebita `markets × regions` per chiamata, quindi
  `h2h,totals` su 1 regione = **2 crediti** a fetch. Dal 06/09 il mercato
  totals (OU) è ESCLUSO dalle selezioni → richiederlo era puro spreco.
  Fix (commit `d1f1c18`): `markets="h2h"` only (tripwire
  `test_ou_exclusion.test_odds_request_solo_h2h`), deploy verificato sul
  container (`8887b972`). Giro serale 09/09 18:00 UTC verificato: 5 partite
  analizzate (La Liga + Serie B) con cache esistenti (stagger fase + TTL
  non scaduto → **0 crediti** consumati dal giro) e settlement da cache
  scores fresche; remaining invariato a 127. Prossimo fetch reale con
  h2h-only alla prima scadenza TTL (10/09 04:00 UTC → 1 credito/lega
  invece di 2). Budget residuo ~127 per ~21 giorni ≈ **6/giorno sostenibili**:
  con settlement 2-3 + value 3,5 (h2h-only) il consumo rientra, ma la
  rotazione va tenuta d'occhio (niente aggiunte fino al reset del 1° ottobre).
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
  Stake **ADATTIVO** di default (`adaptive_staking.py`):
  Kelly frazionato dinamico (0.10-0.35 vs 0.25 fisso prima) con drawdown
  protection (>10% drawdown → riduzione stakes) e confidence weighting
  (market_edge alto + strong_value → stake più alto). Cap: 3% value, 5%
  strong_value. Fallback: stake fisso `BET_STAKE_EUR` se modulo assente.
  **Flat-stake opzionale (09/09)**: con `AUTO_BET_STAKE_MODE=flat` ogni
  segnale +EV del Calcio 1X2 viene piazzato a `AUTO_BET_FLAT_STAKE_EUR`
  (default 1 USDC = minimo ordine SX Bet); i risk cap restano attivi ma
  a UNITA' INTERE (`apply_flat_budget`: max floor(30% bankroll) segni per
  blocco correlato e floor(40% - già piazzato) segni/giorno, EV-decrescenti
  — niente frazioni non piazzabili, il minimo SX è 1 USDC).
  ⚠️ **RIPRISTINATO IL KELLY DINAMICO il 09/09 sera** dopo il deposito
  Bybit→SX a **~47,16 USDC** (verificato live su Railway:
  `availableBalance 47163372`; env `AUTO_BET_STAKE_MODE=adaptive`, env
  flat rimossa). Esempi su 47,16 USDC: value €2,99 / strong_value €5,68
  con i default prudenti (Kelly 0,05-0,40, cap 10%/25%, floor 1 USDC,
  esposizione 40% ≈ €18,9/giorno, correlazione 30% ≈ €14,2/blocco,
  drawdown −10% → stake −50%).
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

### Miglioramenti 10/09/2026

- **Credit watchdog** (`bot.py`): job ogni 6h che legge le cache
  `toa_*.json` e invia alert Telegram sotto le soglie
  (50→warning, 20→alert, 10→danger, 5→critical). Zero costo API.
  Registrato in `main()` con `run_repeating`.
- **Settlement watchdog**: timeout per puntate LIVE >6h senza
  settlement — avviso automatico se una bet `mode='live'` resta
  aperta senza `esito_finale` per oltre 6 ore.
- **GET /api/credits** (`web_api.py`): endpoint REST che restituisce
  remaining_min, sport_cached, days_to_reset, dettagli per sport,
  soglie di allarme e stato operatività.
- **Filtro proattivo crediti** (`odds_api.py`): `should_query_sport()`
  e `get_remaining()` — disattiva automaticamente le leghe a basso
  valore quando i crediti scendono sotto 50/30/15:
  - >=50: tutto attivo
  - <50: solo leghe core (intervallo ≤7gg)
  - <30: solo top 6 leghe
  - <15: solo Serie A, PL, La Liga
- **Tennis validation gate** (`tennis_sandbox.py`): `MIN_OBSERVATIONS=10`
  — se il ledger ha meno di 10 osservazioni, gli stake vengono
   ridotti al 50% e viene registrato un avviso (`is_validated()`).
   Previene scommesse su dati statisticamente insufficienti.
- **ALTER TABLE signals surface** (`tracker.py`): migration idempotente
  che aggiunge la colonna `surface` alla tabella `signals`.
  `log_signal` ora accetta il parametro `surface`.
- **Railway Agent setup**: skills `use-railway` e MCP server
  installati per Claude Code, OpenCode, GitHub Copilot.

### Strategia: aumento frequenza scommesse (10/09/2026)

**Obiettivo**: aumentare il numero di puntate giornaliere abbassando le soglie di qualificazione.

**Modifiche implementate:**
- `MARKET_EDGE_MIN` da 0.03 a **0.02** (+2pp invece di +3pp)
- `EV_MIN` da 0.03 a **0.02** (+2% invece di +3%)
- Nuovo tier **moderate** tra `value` e `strong_value`
  - strong_value: edge >= 5pp (cap 10% bankroll)
  - value: edge >= 2pp (cap 7% bankroll)
  - moderate: edge >= 0pp / EV >= 2% (cap 4% bankroll)
- `confidence_kelly_fraction` esteso con tier moderate (+0.1 score)
- `adaptive_stake` con cap ridotto per moderate (4% fisso)
- `_today_value_picks` include `moderate` nei candidati
- `get_signal_tier()`: nuova funzione di classificazione
- `filter_value_bets`: ora assegna `tier` a ogni segnale
- **MIN_STAKE_EUR**: 1.0 -> **0.01** (rimosso floor 2 EUR, minimo exchange USDC)
- **Notifica FULLY_FILLED** (`bot.py`): dopo ogni giro auto_bet,
  per ogni ordine `status == "FULLY_FILLED"` e `mode == "live"`
  viene inviato un messaggio Telegram in tempo reale con
  partita, esito, quota, stake e bet_id.

**Effetto atteso**: circa **2-3x piu' pick qualificati** (da ~5/giorno a ~10-15/giorno).
Il tier moderate ha cap ridotto (4%) per contenere il rischio sui segnali deboli.
Il sistema mantiene il fail-safe: ogni segnale deve comunque battere il mercato
di almeno 2pp e avere EV >= 2%.

**Notifiche**: l'admin riceve un messaggio Telegram ogni volta che un
ordine LIVE viene FULLY_FILLED, con tutti i dettagli (match, esito,
quota, stake, bet_id).

### Cambio di strategia: STOP + SOLO FAVORITI NETTI (11/09/2026)

**Direttiva del proprietario**: fermare subito le puntate e vietare
**tassativamente** le scommesse su squadre sfavorite/quote alte (anche
sotto a 3.0), riprogettando la logica verso i soli **favoriti netti**
per evitare il rischio di bancarotta.

**1) STOP IMMEDIATO (produzione, verificato sul container)**
- `data/execution/auto_bet_mode.json` = `{"mode": "off"}` sul volume
  Railway → `kill_switch_status()` = {override: off, env_mode: live,
  effective: off, provider_ready: true}; `_execution_mode()` = "off".
  Nessun ordine reale né simulato finché non si usa `/autobet live`.
- **PAUSA SETTLEMENT** (nuovo, `tracker.py`): flag persistente
  `data/execution/settlement_paused.json` (+ env `SETTLEMENT_PAUSED=1`)
  letto da `settlement_paused()`. Con la pausa attiva `settle_bets`,
  `settle_predictions`, `settle_cassa` e `settle_sx_bets` NON chiudono
  nulla e `bot._update_results` esce **prima** di `fetch_scores` (zero
  crediti the-odds-api). Nuovo comando admin
  **`/settlement [on|off|stato]`** per riattivare da Telegram.
  Il tennis sandbox (ledger paper separato) continua a girare: serve
  all'apprendimento ELO e non tocca il bankroll reale.

**2) NUOVO GATE: SOLO FAVORITI NETTI (`value_filter.py`)**
- `ODDS_MAX` da **3.00 → 1.80** (favorito forte, prob. implicita ~55%);
- nuovo `FAVOURITES_ONLY = True` + `MIN_FAVOURITE_MARKET_PROB = 0.50`:
  con prob. di mercato nota l'esito deve essere il **favorito** (≥ 50%);
- nuova `eligible_favourites(candidates)`: tiene solo i candidati con
  prob. di mercato **massima** del mercato, ≥ 50% e quota ≤ 1.80;
  `favourites_gate_reason()` fornisce il motivo standard.
- **Selezione del segnale cambiata**: il candidato giocabile non è più
  il max-EV assoluto ma il **miglior EV tra i favoriti netti**
  (`fixture_engine._analyze_match` e `sx_signals.scan`). Se nessun esito
  qualifica, il match è `rejected` e **non scrive nemmeno una riga** nel
  ledger `predictions`: gli esiti sfavoriti non esistono più per il
  sistema. Il CLV viene registrato solo per i favoriti (niente prezzi di
  esiti scartati nelle medie di chiusura).
- **Difesa in profondità** (`auto_bet._today_value_picks`): il cap quota
  e la prob. di mercato sono rifiltrati a valle, così eventuali righe
  storiche (scritte prima dell'11/09) non possono mai diventare ordini.
- Tier/EV/edge minimi **invariati** (EV ≥ 2%, edge ≥ 2pp, tier
  value/strong_value/moderate come dal 10/09).

**3) Test**: nuovo `test_favourites_only.py` (tripwire di gate su
`value_filter`, `fixture_engine`, `sx_signals`, `auto_bet` — incluso il
caso "riga storica a quota 2.10 → nessun ordine") e nuovo
`test_settlement_pause.py` (flag, env, i tre settle bloccati, riattivazione
che salda ancora, `_update_results` che non scarica risultati in pausa).
`test_value_filter`/`test_market_calib`/`test_sx_signals`/`test_auto_bet*`
aggiornati ai favoriti netti (quote ≤ 1.80, prob. di mercato ≥ 50%).

**Numeri del ledger al momento della decisione** (chiusure per fascia
quota, campioni piccoli): 1.50-1.80 +0.107 (3 chiuse), 1.80-2.00 −0.213
(5), 2.00-2.50 +0.327 (19), 2.50-3.00 −0.196 (20), ≥3.00 −0.158 (22).
⚠️ Nota tecnica: con quote più corte il Kelly calcola frazioni MAGGIORI,
quindi lo stake per singola bet sale.

**4) CAP STAKE SEVERO (stessa direttiva)**: `adaptive_staking` scende da
7%/10% a **1% del bankroll per value e moderate** e **2% per
strong_value** (default di codice; env Railway `STAKE_CAP_PCT=0.01` e
`STAKE_CAP_PCT_STRONG=0.02`, entrambe in `preserve()` in
`.railway/railway.ts`). Il vecchio cap fisso 4% dei moderate è rimosso:
i segnali deboli non possono valere più dei value. `value_filter.MAX_STAKE_PCT`
allineato a 1% (era 3%) così i tool (schedina/`/value`) mostrano lo stesso
cap che il bot applica.

**5) CAP SEVERO VINCOLANTE — `STAKE_CAP_HARD` (11/09, `auto_bet.py`)**:
prima il floor dell'exchange vinceva sul cap (`pick_stake = max(stake,
1.0)`) → con 38 USDC nel wallet ogni bet era 1 USDC = **2.6%**, non 1%.
Ora, con `STAKE_CAP_HARD` attivo (**default**), una bet il cui stake
cappato è sotto il minimo ordine viene **SALTATA (fail-closed)**, sia in
FASE 1 (Kelly/cap) sia dopo i risk cap (correlazione 30% / esposizione
totale 40%). Conseguenza operativa da conoscere: con bankroll < 100 USDC
il cap 1% è sotto 1 USDC → **nessun ordine parte** finché il wallet non
cresce (≥ 100 USDC per il 1%, ≥ 50 USDC per il 2%) oppure finché non si
accetta il floor con `STAKE_CAP_HARD=0`. `/autobet` mostra il cap severo e
avvisa se il saldo attuale non lo sostiene. Env dichiarata in
`preserve()` in `.railway/railway.ts`. Tripwire:
`test_favourites_only.TestStakeCapSevero` (incl. wallet 38 USDC → zero
ordini) e `test_auto_bet_live` (hard ON = salta, OFF = floor 1 USDC).

### Fix strutturali critici 11/09/2026 (mapping leghe + guardia cassa)

**1) FIX MAPPING LEGHE (`sx_signals.py`) — il bug che bloccava il
settlement e lasciava le bet aperte.** Il fuzzy `SequenceMatcher >= 0.55`
restituiva SEMPRE il nome più simile, anche quando era sbagliato. Sample
reale delle etichette SX (API pubblica, ~40 label) con i mapping vecchi:
`Major League Soccer -> League One`, `German Bundesliga -> Austrian
Bundesliga`, `Jupiler League -> Premier League`, `K1-League -> J1 League`,
`LigaPro -> 3. Liga`, `Primera Nacional -> Primeira Liga`, `Primera A ->
Primeira Liga`, `Primera Division -> Primeira Liga`, `First League ->
A-League`, `K2-League -> K League 1`. Conseguenza: il settlement
interrogava the-odds-api sulla competizione SBAGLIATA, non trovava mai il
punteggio e le bet `sx-*` restavano aperte per sempre.
- Nuova tabella `SX_LEAGUE_ALIASES` DETERMINISTICA (etichetta SX → chiave
  `SPORTS_MAP`, con `None` = "competizione non coperta": mai indovinare).
  Copre le 39 label reali osservate (`MLS`, `Liga Profesional -> Argentina
  Primera`, `Jupiler League -> Belgian First Div`, `K1-League -> K League
  1`, `The Championship -> EFL Championship`, `Superliga -> Superliga
  Danimarca`, `Europa League_UEFA -> Europa League`, ecc.).
- Risoluzione a 3 stadi in `_league_sx_to_sports_map`: alias → chiave
  `SPORTS_MAP` esatta → fuzzy STRETTO (`_strict_fuzzy_league`: match exact
  1.0, prefisso/suffisso di paese 0.97, altrimenti similarità ≥ 0.92 o
  token contenuti, con guardia di ambiguità ≥ 0.08 sul secondo). Esiti
  ambigui (`Serie B` Italia vs Brasile, `Super League`) → `None`.
- Nuovo resolver `league_to_sport(league)`: chiave `SPORTS_MAP`, etichetta
  SX grezza o alias → sport key. Usato dal settlement (`sx_signals` e
  `bot._update_results`), che ora LOGGA le leghe non mappate invece di
  saltarle in silenzio.
- `_results_from_the_odds_api` raggruppa i match per sport key RISOLTO e
  matcha i nomi sia stretti sia loose (`_loose_team`): 'FC Cincinnati'
  aggancia 'Cincinnati' senza invertire casa/trasferta.
- `repair_sx_leagues()` + CLI `venv/bin/python sx_signals.py repair`:
  rilegge i mercati attivi da SX (API pubblica, zero crediti) e riscrive
  `matches.league` delle partite `sx-*`, recuperando le righe salvate col
  fuzzy vecchio (il `scan()` ogni 15' fa già self-heal delle partite in
  finestra, perché `save_match` è INSERT OR REPLACE).
- Tripwire: `test_league_mapping.py` (alias reali, rifiuti espliciti,
  fuzzy stretto, resolver, settlement con lega alias e con lega non
  mappata → 0 chiamate API + bet aperta, repair).

**2) GUARDIA CASSA con finestra temporale (`tracker.settle_cassa`).**
La cassa aggancia le bet ai risultati per NOME (non per id): la stessa
coppia di squadre può comparire più volte (stagione precedente,
andata/ritorno) e il vecchio codice prendeva la riga "più recente" senza
limite. Ora:
- ogni candidato per coppia normalizzata è raccolto (non solo il più
  recente) e la scelta avviene per **vicinanza temporale** alla data della
  bet (`data`, in fallback `timestamp`);
- il risultato viene usato solo entro `CASSA_MATCH_WINDOW_DAYS` (default
  **14**, env) dalla bet; fuori finestra la riga **resta in gioco** e
  viene loggata (`out_window`);
- fallback storico (il più recente) SOLO se non esiste alcuna data
  utilizzabile.
- Tripwire: `test_cassa_window.py` (risultato vecchio bloccato, scelta del
  più vicino, env override, fallback senza date, idempotenza).

**3) BUG INCIDENTALE TROVATO E FIXATO — `STALE_INPLAY_HOURS` non
definita (`odds_api.py`).** `_cache_is_stale_for_settlement` usava la
costante ma non era mai stata definita → `NameError` inghiottito
dall'`except` → la cache punteggi "in corso da ore ma completed=False"
veniva considerata valida e il settlement restava bloccato (un'altra
causa di bet aperte). Aggiunta `STALE_INPLAY_HOURS = 3` (un match di
calcio finisce entro ~2h). `test_scores_cache_stale` torna verde.

**4) `backtest_mc` minimo stake allineato alla produzione.**
`_simulate_sequence` scartava gli stake `< 2.0` (minimo Exchange Italia);
con il cap severo 1% su bankroll 100 lo stake è 1.0 (floor SX) → TUTTE le
bet venivano scartate e la simulazione dava 0. Nuova `MIN_STAKE` (env
`MIN_STAKE_EUR`, default 1.0). `test_backtest_mc` torna verde.

**5) SIMULAZIONE STAKE NEL BACKTEST (`historical_backtest.py`).** Nuovo
flag `--production`: applica la strategia di produzione (gate
`is_sane(favourites_only=True)`, `--max-odds` default 1.80, `--no-ou`
implicito) e usa lo stake Kelly 1/4 con cap 1% di `value_filter.kelly_euro`.
Risultati sul dataset cached (16.273 partite, `--no-ensemble`):
- **modalità PRODUZIONE: 0 bet** — nel mercato storico (quote di
  chiusura/Pinnacle) il gate favoriti + EV ≥ 2% non trova alcun valore: il
  blend pesa il mercato e sui favoriti il modello NON batte la closing
  line. Conferma il motivo dei pochi segnali live.
- baseline di ricerca (universo `MAX_ODDS` 5.0): 7.861 bet, ROI −8.27%,
  MaxDD 99.8% (bankroll 100 → 3.08); `by_market` OU −6.85%, 1X2 −14.73%.
- 1X2-only (`--no-ou`) flat: 2.619 bet, ROI −9.95%, hit 27.8%, avg quota
  3.41 (universo di ricerca, NON il gate favoriti).

Stato test: tutti i file verdi (inclusi i 4 nuovi/aggiornati); la suite
completa resta lunga (>10 min) → girare a file/capitoli.

### Guardrail di rischio 11/09/2026 (quota minima, edge, stop-loss, liquidità)

Direttiva del proprietario: rendere il sistema più prudente sui favoriti.
Quattro protezioni aggiunte, tutte con env override (nessun redeploy di
codice) e tripwire dedicati in `test_risk_guards.py`.

**1) QUOTA MINIMA `ODDS_MIN` 1.50 → 1.30 (`value_filter.py`).**
Insieme a `ODDS_MAX` (1.80) definisce la fascia dei favoriti netti
**1.30–1.80**: sotto 1.30 il ritorno per unità di stake non compensa il
rischio (probabilità implicita > 77%). Sotto soglia `is_sane` rifiuta con
"quota troppo bassa". Il messaggio di `favourites_gate_reason()` e il
display webapp (`webapp/app/value/page.tsx`) sono allineati.

**2) EDGE MINIMO +3pp (`market_calib.py`).** `MARKET_EDGE_MIN` e
`MARKET_EDGE_MODERATE` riportati da 0.02 a **0.03** (+3pp di probabilità
stimata vs mercato devigato): il bot NON punta il favorito "alla cieca",
deve battere il mercato. `MARKET_EDGE_STRONG` resta +5pp per i
`strong_value`. ⚠️ Contro la direttiva del 10/09 (che li aveva abbassati a
+2pp per aumentare la frequenza): ora la priorità è la prudenza, non il
volume. `EV_MIN` resta 2%.

**3) STOP-LOSS GIORNALIERO (`auto_bet.py`).** Se il bankroll scende del
**5%** (`DAILY_STOP_LOSS_PCT`) rispetto al valore registrato a inizio
giornata, le puntate vengono **bloccate per 24h** (`DAILY_STOP_HOURS`).
- Stato persistente sul volume (`data/execution/daily_stop.json`, scrittura
  atomica): sopravvive ai redeploy, si ri-arma al primo giro del giorno
  successivo.
- In LIVE il bankroll è il saldo REALE del wallet SX (rischio = denaro
  vero); in SIM la cassa. Fail-open: un file corrotto NON blocca le
  puntate, ma un blocco attivo resta rispettato.
- Hook in `run_today_bets` (check subito dopo la determinazione del
  bankroll) + `daily_stop_status()` per `/autobet` + alert Telegram
  una-volta-al-giorno nel job (chiave `DAILY_STOP`, anti-spam col giro ogni
  60s) + `clear_daily_stop()` per la rimozione manuale.

**4) FILTRO LIQUIDITÀ SX (`sx_signals.py` + `auto_bet.py`).** Su un
exchange la quota mostrata può non essere disponibile: due guardie.
- **Generazione segnali** (`sx_signals.scan`): `SX_MIN_DEPTH_USDC`
  (default 15) sulla profondità totale del match e
  `SX_MIN_LEG_DEPTH_USDC` (default 5) sulla profondità minima del singolo
  esito → i mercati sottili non generano segnali.
- **Prima dell'ordine** (`auto_bet._live_available_size`): la size BACK
  disponibile al floor deve coprire lo stake, altrimenti l'ordine viene
  saltato (rischio slippage/riempimento parziale). Se il book non è
  leggibile o è in formato ignoto NON blocca (fail-open sulla lettura,
  fail-closed sulla size reale).

Stato test: `test_risk_guards.py` (nuovo) + `test_value_filter`,
`test_favourites_only`, `test_auto_bet*`, `test_sx_signals`,
`test_market_calib` tutti verdi.

### Verifica forzata dei guardrail + monitor scarti liquidita' (11/09/2026)

**1) VERIFICA FORZATA (`verify_guardrails.py`).** Script di diagnostica che
dimostra — con i log REALI e senza toccare la produzione (DATA_DIR e DB
temporanei, mai un provider reale) — che i guardrail bloccano davvero le
puntate. Esito ultimo giro: **tutti i guardrail BLOCCANO** (A-E).
- **A. Kill-switch OFF** → `modalita' 'off' (kill-switch o fail-closed):
  nessuna puntata`, 0 ordini.
- **B. Stop-loss -5%** → `STOP-LOSS GIORNALIERO — perdita 6.0% (>= 5%):
  puntate bloccate fino a ...`, 0 ordini anche se il bankroll risale.
- **C. Cap severo 1%** → con wallet 38 USDC lo stake Kelly×cap = 0.38 <
  minimo ordine 1.0: `CAP SEVERO: stake cappato 0.38 USDC < minimo ordine
  1.00 USDC ... ordine saltato (fail-closed)`, 0 ordini inviati al
  provider. Controprova con `STAKE_CAP_HARD=0`: il floor viene accettato
  e l'ordine parte (1 USDC).
- **D. Quote 1.30-1.80** → quota 1.90 scartata (`quota > 1.80`), quota
  1.20 scartata (`quota < 1.30: fascia favoriti 1.30-1.80`, gate aggiunto
  come difesa in profondita' in `auto_bet._today_value_picks`), esito non
  favorito scartato (`prob. mercato < 0.50`).
- **E. Liquidita' SX** → con stake 5 USDC i richiesti sono
  `max(5 × 2.0, 25.0) = 25.0` USDC al floor: book da 4 USDC →
  `liquidita' 4.00 < richiesta 25.00 ... salto (rischio slippage)`, 0 ordini
  inviati e scarto `order/depth_vs_stake` nel monitor; controprova con book
  da 26 USDC → l'ordine parte (1 chiamata al provider).
Uso: `venv/bin/python verify_guardrails.py` (exit 0 = tutto bloccato).

**2) MONITOR SCARTI LIQUIDITA' (`liquidity_monitor.py`, nuovo).**
Su SX Bet (exchange) uno scarto per book sottile e' un'edge potenzialmente
persa: il monitor la rende misurabile invece che silenziosa.
- **Log JSONL sul volume** (`data/execution/liquidity_skips.jsonl`, path
  overridabile con `LIQUIDITY_SKIP_LOG`). Tre tipi di evento:
  `scan` (segnale non generato: `sx_signals.scan`), `order` (ordine reale
  saltato: `auto_bet._live_fill`) e `partial` (riempimento parziale
  materializzato). Per gli scarti d'ordine registra anche l'**edge NON
  realizzato** (`EV × stake`), la metrica che quantifica il costo.
- **Bot**: job `liquidity_monitor_job` ogni 6h che logga SEMPRE lo stato e
  allerta admin+iscritti (anti-spam 1/giorno, chiave `LIQ_SKIP`) solo se ci
  sono scarti nelle 24h; sezione `💧 Scarti liquidita' SX` nel report
  giornaliero. Zero costi API (legge solo il JSONL).
- **CLI**: `venv/bin/python liquidity_monitor.py [--days N] [--json]`.
- Fail-safe totale: `record_skip`/`iter_events` non propagano mai eccezioni
  (un log corrotto o non scrivibile non blocca mai il giro puntate).
- Test: `test_liquidity_monitor.py` (12+ test: riepilogo/finestra/righe
  corrotte/fail-safe/scan/order/partial/report/tripwire job/difesa
  quota-min a valle).

Stato test: focus verdi — `test_liquidity_monitor`, `test_risk_guards`,
`test_bot`, `test_sx_signals`, `test_auto_bet_live`, `test_reports`,
`test_web_api`, `test_tier`, `test_performance_report`.

### Taratura soglie di liquidita' SX (11/09/2026, secondo giro)

Obiettivo del proprietario: il bot deve **rifiutare da solo** gli ordini su
mercati SX Bet senza liquidita' sufficiente a eseguire la quota richiesta
senza slippage. La pagina visiva degli scarti arrivera' dopo: qui si sono
tarate le soglie (tutte da env, zero redeploy di codice).

**1) GUARDRAIL D'ORDINE (`auto_bet.py`) — la parte vincolante.**
Prima bastava `profondita' al floor >= stake` (copertura secca: lo stake
poteva esaurire il lato del book). Ora il vincolo e' **copertura con
margine**:

```
richiesto = max(stake × SX_DEPTH_MULTIPLIER (2.0), SX_MIN_EXEC_DEPTH_USDC (25.0))
```

- `SX_DEPTH_MULTIPLIER` (default **2.0**): lo stake non deve consumare piu'
  della meta' del lato visibile al floor — altrimenti il resto della size
  muove il prezzo e la quota "richiesta" non e' piu' garantita.
- `SX_MIN_EXEC_DEPTH_USDC` (default **25.0**): soglia ASSOLUTA di libro al
  floor per qualunque stake (un mercato quasi vuoto non e' negoziabile
  nemmeno con 1 USDC).
- Nuova helper `auto_bet.required_depth(stake)` (unica fonte della formula,
  testata); il salto e' fail-closed e finisce nel monitor con
  `threshold = richiesto`, `extra.richiesto/multiplier/min_exec_depth`.
- Fail-open SOLO sulla lettura del book (formato ignoto/provider senza
  book): resta fail-closed sulla size reale misurata.
- Esempi: stake 1-5 USDC → servono 25 USDC al floor; stake 20 USDC → 40
  (il multiplo prevale: max(40, 25)).

**2) GUARDRAIL DI SCAN (`sx_signals.py`) — coerenza con l'ordine.**
- `SX_MIN_DEPTH_USDC` 15 → **25** (liquidita' TOTALE delle 3 leg: salute
  del mercato).
- `SX_MIN_LEG_DEPTH_USDC` resta **5** (ogni singolo esito esiste).
- NUOVO `SX_MIN_EXEC_DEPTH_USDC` (**25**, stessa env e stesso default di
  `auto_bet`): la **leg che verrebbe giocata** (il favorito scelto) deve
  avere almeno 25 USDC al floor. Sotto soglia il match non produce NESSUN
  segnale (`continue` prima di `save_match`): un segnale non eseguibile
  sarebbe rumore nel ledger e nel CLV. Scarto registrato come
  `scan/depth_exec_<esito>`.
- Effetto: le soglie di mercato restano morbide sulle leg NON giocate
  (l'underdog sottile non elimina piu' un match giocabile sul favorito),
  mentre la leg giocata e' allineata 1:1 al guardrail d'ordine.

**3) MONITOR (`liquidity_monitor.py`).** Default riallineati (25/5/25/×2.0),
nuovo blocco `thresholds` in `summary()` e riga "Soglie attive" nel report
Telegram; il messaggio di allerta finale elenca anche le nuove env.

**4) ENV in `preserve()`** (`.railway/railway.ts`): `SX_MIN_EXEC_DEPTH_USDC`
e `SX_DEPTH_MULTIPLIER` aggiunte accanto alle altre soglie liquidita', cosi'
`railway config apply` non le distrugge.

**Test**: `test_risk_guards.py` (soglie gemelle scan/ordine +
`required_depth` = max(multiplo, minimo), fallback su stake non numerico),
`test_liquidity_monitor.py` (book 6 USDC con stake 5 → salto per margine e
evento con `threshold`/`extra.richiesto`; scan con leg giocata a 6 USDC e
totale 86 → `depth_exec_1`), `verify_guardrails.py` (scenario E).
Tutti verdi nel focus: risk_guards + liquidity_monitor + sx_signals (39),
auto_bet* + favourites_only + value_filter + market_calib + tier + reports
(132), test_bot (20).

### Misura dell'impatto delle soglie (11/09/2026, `liquidity_impact.py`)

Prima di riattivare il bot: **quante puntate bloccano davvero le nuove
soglie?** Nuova diagnostica `liquidity_impact.py` (solo LETTURA: API
pubblica SX, zero crediti, zero ordini, nessuna scrittura sul ledger —
tripwire in `test_liquidity_impact.py`). Misura DUE grandezze che non vanno
confuse:
- **profondita' di mercato** di un esito = somma di TUTTI i livelli
  (quella usata dal filtro di `sx_signals.scan`);
- **size al floor** = somma delle size al prezzo migliore (quella usata dal
  guardrail d'ordine `auto_bet._live_available_size`).

CLI: `venv/bin/python liquidity_impact.py [--hours 72] [--max-events 200]
[--with-model] [--json] [--save file.json] [--from-cache file.json]`
(campione congelato in `data/execution/liquidity_impact_sample.json`).

**ISOLAMENTO DELLE VARIABILI (direttiva 11/09)**: il default misura
**SOLO la liquidita'**. Con `with_model=False` il modello non viene nemmeno
interrogato (`_row_for_event` esce prima di `expected_goals`): la selezione
del favorito dipende solo dal mercato (devig + fascia quota), quindi nessun
numero del conteggio scarti puo' essere inquinato da ratings/EV. Il gate
modello esiste come sezione **opt-in** (`--with-model`, variabile separata da
misurare quando i ratings saranno allineati). Tripwire:
`test_liquidity_impact` (default senza chiavi modello, modello mai chiamato,
verdetto solo-liquidita').

**Campione reale 11/09/2026 (197 partite, finestra 72h, tutte le leghe SX):**
- 177 partite coerenti (inv_sum 0.98-1.08).
- Filtro di MERCATO: **VECCHIE soglie 177/177 (100%), NUOVE 177/177 (100%),
  bloccate 0 (0.0%)**.
- 42 candidati favoriti (fascia 1.30-1.80 + prob. mercato): **42/42 (100%)
  eseguibili** col guardrail d'ordine.
- Sensibilita' allo stake (richiesto = max(stake×2, SX_MIN_EXEC_DEPTH_USDC)):
  stake 1-10 USDC **42/42**, stake 20 USDC 41/42 (97.6%).
- Se irrigidissimo la soglia della leg giocata: 25 USDC → 42/42, 50 → 41/42,
  100 → 38/42 (90.5%). La liquidita' dei 1X2 calcio su SX e' ABBONDANTE
  (size al floor: p10 102, p50 263, p90 986 USDC).
- **Verdetto: la liquidita' NON e' il collo di bottiglia**; le soglie sono
  prudenti ma non restrittive (si potrebbe alzare la soglia senza uccidere
  il flusso). Riga esplicita nel report: **SCOMMESSE PERSE PER LIQUIDITA'
  = 0/42 (0.0%)**.
- **TARATURA FINALE (decisione 11/09 sera)**: `SX_MIN_EXEC_DEPTH_USDC`
  portata da **10 → 25 USDC** su indicazione del proprietario, proprio
  perche' la misura mostrava che alzare la soglia NON taglia opportunita'
  (nella sensibilita' del misuratore 25 USDC → 42/42 eseguibili, 100%).
  Vale insieme in `sx_signals` (scan) e `auto_bet` (ordine): piu'
  protezione da slippage a impatto nullo sul flusso. `SX_DEPTH_MULTIPLIER`
  resta 2.0; totale match 25 ed esito singolo 5 invariati.

**⚠️ CAVEAT IMPORTANTE — GATE MODELLO non misurabile in locale.** Il DB
locale ha **0 righe in `team_ratings`** (i rating vivono sul volume Railway):
`expected_goals` usa quindi il profilo neutro di lega e il modello non
rappresenta la produzione. Nel campione il gate modello da' 0 segnali value,
ma QUEL numero va misurato sul container. Lo script ora rileva la cosa da
solo: campo `model_gate.measurable` + avviso esplicito nel report quando la
copertura ratings e' 0%.

### Copertura rating sul container + gate modello (11/09/2026, SOLA LETTURA)

Verifica eseguita con `railway ssh --service api -- python3 -c ...`:
sqlite aperto in **`mode=ro`** (URI read-only), nessun ordine, nessuna
scrittura sul ledger, zero crediti the-odds-api (discovery SX pubblica).

**1) ENV su Railway**: `SX_MIN_DEPTH_USDC`, `SX_MIN_LEG_DEPTH_USDC`,
`SX_MIN_EXEC_DEPTH_USDC`, `SX_DEPTH_MULTIPLIER` **non sono impostate**
(`os.getenv` → None) → valgono i default di CODICE. Quindi la soglia 25 e'
attiva appena il codice viene deployato (nessuna env da aggiornare);
`preserve()` in `.railway/railway.ts` serve solo a non farle distruggere se
un giorno verranno impostate.

**2) COPERTURA RATING (il collo di bottiglia vero del gate modello).**
`team_ratings` = **200 squadre / 14 leghe** (ultimo aggiornamento
11/09 01:49), `match_results` = 2725. Profonde solo 9 leghe: MLS 39, Serie A
24, La Liga 23, Eredivisie 19, Premier League 18, EFL Championship 18,
Brasileirao 18, Ligue 1 17, Bundesliga 16; le altre 5 leghe hanno 1-3
squadre (Primeira Liga 1, Champions League 0, Liga MX 0, Russian Premier
League 0, Brazil Serie B 0). Il ledger SX ha 69 partite: **5 con entrambe le
squadre rated (4 con n>=5)**, 19 con una sola, **45 con nessuna**.
Ripartizione SX (n / con entrambe rated): Primeira Liga 10/0, Premier League
7/1, Champions League 6/0, Brazil Serie B 5/0, League One 5/2, Russian
Premier League 5/0, Sweden Superettan 4/0, Liga MX 4/0, Austrian Bundesliga
4/0, Allsvenskan 3/0, Serie B 3/0.

**3) GATE MODELLO LIVE (stesso giro, mercati freschi)**: 39 mercati SX,
36 coerenti (inv_sum 0.98-1.08), **3 candidati favoriti** (fascia 1.30-1.80).
Solo **3/36 coerenti (8.3%)** hanno entrambe le squadre con rating;
**nessuno dei 3 favoriti** ha rating. Edge modello sul favorito di mercato:
mediana **-24.1pp** (p10 -26.5, p90 -13.5), **0/3 con edge >= 0** → **0
segnali value** a qualunque soglia (2pp/3pp/5pp). Interpretazione: su squadre
senza rating `expected_goals` usa il profilo NEUTRO di lega, che su un
favorito netto (quota 1.30-1.80) da' una probabilita' molto inferiore a
quella del mercato: l'edge negativo e' **cecita' del modello**, non un
segnale di valore. Il gate modello NON e' misurabile in modo utile finche'
la copertura resta questa.

**4) SECONDO GAP: NORMALIZZAZIONE DEI NOMI** (anche nelle leghe coperte).
Confronto SX ↔ `team_ratings` sulle partite SX delle leghe coperte — i nomi
spesso NON combaciano (ratio difflib): `Wrexham` vs `Wrexham AFC` (0.78),
`AFC Bournemouth` vs `Bournemouth` (0.85), `Ipswich Town` vs `Ipswich`
(0.74), `Willem II Tilburg` vs `Willem II` (0.69), `Tottenham Hotspur` vs
`Tottenham` (0.69), `AS Monaco FC` vs `Monaco` (0.67),
`Olympique Marseille` vs `Marseille` (0.64); `Nottingham Forest` assente.
Nota collaterale: nel ledger `matches` compaiono righe con league
`Premier League` per club belgi (KV Mechelen, RSC Anderlecht) — residuo di
un mapping pre-fix (il ledger viene riscritto solo quando lo scan rivede il
match: il `repair_sx_leagues()` lo sana).

**5) Conseguenza operativa**: prima di fidarsi del gate modello servono
(a) ratings estesi alle leghe che SX scansiona davvero (CL, Primeira, Liga
MX, Austria, Svezia, Russia, Brazil B, ...) e (b) una mappa alias nomi
squadra SX↔API-Football (stesso pattern deterministico di
`SX_LEAGUE_ALIASES`, mai fuzzy). **→ FATTO l'11/09 sera** (vedi "Fix cecita'
modello: 3 passi"): (b) e' `team_names.py` (coverage 5/69 -> 15/69) e (a)
e' la coppia rosters (`ALL_LEAGUES` 35 -> 47) + `LEAGUE_IDS` (8 -> 41);
resta da eseguire la sync quando l'account API-Football sara' riattivato.
Solo dopo ha senso rilanciare `liquidity_impact.py --with-model` sul
container per numeri rappresentativi.

### Fix cecita' modello: 3 passi (11/09/2026)

Direttiva del proprietario: fermare tutto e chiudere i tre gap emersi dal
report, verificando IN LOCALE prima di pensare al deploy.

**PASSO 1 — repair del ledger (`sx_signals.repair_sx_leagues`).**
Il repair leggeva l'etichetta SX solo per le partite ANCORA sui mercati: i
residui storici (es. 5 partite MLS salvate come `League One` dal fuzzy
vecchio) restavano sbagliati per sempre. Ora c'e' un fallback che deduce la
lega dai ROSTER (`ALL_LEAGUES`) via `_infer_league_from_teams(home, away,
current)`, e la risoluzione nomi (`team_names`) fa agganciare anche i nomi SX
reali (`Atlanta United` -> roster `Atlanta`, `Toronto FC` -> `Toronto`).
Regola PRUDENTE: si sovrascrive SOLO se la lega attuale NON ha nessuna delle
due squadre nel roster (`_roster_support(current) == 0`) e se ESATTAMENTE
una lega contiene entrambe — altrimenti non si tocca nulla. Prova del
perche': con una regola piu' aggressiva il repair aveva trasformato
`Champions League` in `Europa League` per Fenerbahçe–AS Roma (roster CL
incompleto ma etichetta SX corretta). Verificato in locale: `League One` ->
`MLS` su 5/5 residui, `Champions League` intatto, secondo giro `updated 0`
(idempotente). CLI: `venv/bin/python sx_signals.py repair`.

**PASSO 2 — risoluzione nomi squadra (`team_names.py`, nuovo).**
Quattro stadi deterministici, mai un indovinello: (1) match esatto,
(2) `TEAM_ALIASES` espliciti (Nottingham Forest -> Nottm Forest, Spurs ->
Tottenham, PSG -> Paris Saint-Germain, ...), (3) chiave normalizzata
(minuscole, senza accenti/punteggiatura, senza token societari FC/AFC/AC/AS/
KV/RSC/..., senza numeri di fondazione, token ordinati), (4) contenimento di
token con GUARDIA DI AMBIGUITA' (due candidati troppo vicini -> None).
Innescato in `rating_engine.get_rating` (`resolve_team_name`): da li' lo
usano `poisson_engine.expected_goals`, `sx_signals.scan`, `auto_bet` e
`liquidity_impact`. Fail-safe totale: qualunque errore o dubbio ricade sul
nome originale (profilo neutro), mai su un rating sbagliato.
Misura sui DATI REALI del container (69 partite SX, 200 nomi rating):
partite con ENTRAMBE le squadre rated **5/69 (7.2%) -> 15/69 (21.7%)**, 3x.
Le non risolte sono squadre ASSENTI da `team_ratings` (Brazil Serie B,
Superettan, CL: Fenerbahçe, Shakhtar, Slavia Praha...): e' il gap di
copertura del passo 3, non un problema di nomi.

**PASSO 3 — copertura leghe (`football_hist.py` + `leagues_data.py`).**
`LEAGUE_IDS` passa da 8 a **41 leghe**: coppe europee (CL 2, EL 3,
Conference 848, Libertadores 13, Sudamericana 11), cadetterie e campionati
che SX scansiona (Championship 40, League One 41, Serie B 136, Primeira Liga
94, Liga MX 262, J1 98, K League 1 292, Saudi 307, Swiss 207, Superliga DK
119, Allsvenskan 113, Eliteserien 103, A-League 188, Veikkausliiga 244,
Argentina 128, Chile 265, Colombia 239, Egitto 233) piu' le 10 chiuse
l'11/09 sera (Austria 218, Russia 235, Turchia 203, Belgio 144, Scozia 179,
Grecia 197, Polonia 106, 3. Liga 80, Superettan 114, Brazil Serie B 72).
**Roster aggiunti in `ALL_LEAGUES` (35 -> 47 leghe):** Austria, Russia,
Turchia, Belgio, Scozia, Grecia, Polonia, 3. Liga, Superettan, Brazil Serie
B, League One, Copa Sudamericana (~150 squadre con profili prior, costruiti
con l'helper `_profiles`). Senza roster `_match_db_name` non allinea nessuna
squadra: la sync brucerebbe richieste salvando 0 righe. Tripwire:
`test_football_hist.test_ogni_lega_sincronizzabile_ha_un_roster` (ogni id in
`LEAGUE_IDS` DEVE avere un roster non vuoto) + `test_leghe_dei_buchi_ora_in_
all_leagues`. In piu' `LEAGUE_EFFICIENCY` (market_calib) ha ora gli score
delle nuove leghe per il blend dinamico. Aggiunte tre difese:
- **validazione id→lega** (`LEAGUE_API` + `_league_response_ok`): la sync
  confronta nome/paese restituiti dall'API con l'atteso e SALTA la lega se
  non combaciano — un id sbagliato non puo' importare un'altra competizione
  sotto il nome sbagliato;
- **marker di sincronizzazione** (tabella `sync_state`, per lega+stagione,
  scritti SOLO dopo il salvataggio): niente piu' 30 leghe riscaricate ogni
  giorno (free plan 100 req/giorno). `FORCE_HISTORY_SYNC=1` li ignora;
- **memo stagioni** in-process: la prima lega scopre dove comincia la
  copertura del piano, le successive partono da li' (2 richieste/lega in
  meno). Nuova CLI `--verify-ids` (una chiamata per lega, nessuna scrittura)
e `--reset-markers`.

**⚠️ BLOCCO ESTERNO SUL PASSO 3 — account API-Football SOSPESO.**
Verificato sia in locale sia SUL CONTAINER con una chiamata reale a
`/leagues`: `{'access': 'Your account is suspended, check on
https://dashboard.api-football.com.'}` (chiave presente, 32 char, ma
rifiutata; `--verify-ids` -> 0/30). Quindi il job giornaliero
`history_sync_job` (08:30 ITA) e' fermo da tempo: ecco perche' le leghe
extra sono vuote. La sync NON ha mai importato dati sbagliati (salta la
lega e logga), ma finche' l'account non viene riattivato il passo 3 resta
*implementato e non eseguito*: dopo la riattivazione basta
`venv/bin/python football_hist.py --verify-ids` e poi `/sync` (o
`football_hist.py --seasons 2`).

**STATO CONTAINER (11/09 sera)**: prima del deploy il container girava codice
SENZA `SX_LEAGUE_ALIASES`, `repair_sx_leagues`, `team_names` (verificato via
`railway ssh`: attributi assenti). Dopo il push (deploy automatico Railway)
il repair si esegue SUL container, cosi' sana anche i residui del volume:
`railway ssh --service api -- python3 sx_signals.py repair`. Il repair ora
usa l'inferenza dai roster: i residui belgi salvati come `Premier League`
(KV Mechelen, RSC Anderlecht) diventano `Belgian First Div` — verificato in
locale (`_infer_league_from_teams('KV Mechelen','RSC Anderlecht',
'Premier League') -> 'Belgian First Div'`).

**Verifica finale in locale (prima del deploy)**: 130 test verdi sul set
toccato (league_mapping, team_names, football_hist, odds_api, rating_engine,
poisson_engine, sx_signals, market_calib); tutti gli id hanno roster; nessun
roster vuoto in `ALL_LEAGUES`.