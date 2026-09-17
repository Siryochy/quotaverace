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
8. **Push su `main` = deploy IMMEDIATO → prima di pushare controllare i
   marker di conflitto**: `grep -rn "^<<<<<<<" *.py` deve essere vuoto. Il
   13/09 un conflitto di merge committato in `auto_bet.py` ha messo giù
   l'INTERA produzione (502, bot fermo, zero settlement) e nessuno se ne e'
   accorto per ore. I test vanno eseguiti PRIMA del push.

## Segreti: vault cifrato (`secrets/`)

- Tutti i segreti locali vivono in `secrets/vault.bin` (Fernet + PBKDF2,
  `SECRETS_MASTER_KEY` nel `.env` gitignored, chmod 600). Mai plaintext nel
  repo, mai loggati, caricati solo in memoria da `secrets_store.py` al
  bootstrap (`config.py` → `load_secrets_dir`).
- **Rotazione chiave (12/09/2026)**: `SECRETS_MASTER_KEY` ruotata (la vecchia
  era esposta in chat) — nuovi segreti cifrati con chiave fresca.
  Vecchia `secrets/betfair/` (cert SSL) rimossa.
- CLI: `venv/bin/python secrets_store.py vault|check|get NOME`. Per aggiungere
  un segreto: file plaintext in `secrets/` → `vault --commit` (cancella il
  plaintext). Se perdi `SECRETS_MASTER_KEY` senza plaintext, i segreti sono
  persi.
- Su Railway i segreti restano nelle env vars del progetto (cassaforte vera).
- `vault --commit` NON è ricorsivo: considera solo i file
  diretti in `secrets/` (`iterdir`). MAI mettere `*.key`/`*.pem`
  direttamente in `secrets/`. (Cartella `secrets/betfair/`
  rimossa il 12/09.)
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

## Stato attuale (aggiornato al 12/09/2026)

- **SETTLEMENT NATIVO SX (12/09, deploy `0305ffc`, VERIFICATO IN PRODUZIONE)**:
  le leghe che SX scansiona ma the-odds-api NON copre (Primera A Colombia,
  Primera Nacional, K2-League) lasciavano bet/previsioni aperte per sempre —
  nessuna fonte esterna puo' refertarle. Ora `_results_from_sx` (sx_signals.py)
  legge l'esito DALL'EXCHANGE, gratis e senza matching per nome:
  1) `markets/find` sui market_hash salvati sulle bet (batch da SX_FIND_BATCH=30,
     docs: max 30 hash/chiamata): ogni mercato binario porta SEMPRE i punteggi
     dell'evento (teamOneScore/teamTwoScore) e, se saldato, `outcome` — la
     semantica e' relativa alla GAMBA ("T1 vs Not T1": outcome 1 = vince T1),
     quindi il verdetto 1X2 lo emette SEMPRE settle_bets/settle_predictions dai
     punteggi (fail-closed, mai dal campo outcome della gamba). Si saldano
     anche le bet ORFANE senza riga in `matches` (i nomi vengono dalla find;
     la find riporta anche la lega vera: America MG–Nautico era Serie B).
  2) `/markets/active` per i match con riga nel ledger: punteggi live usati
     come finali SOLO a >= SX_LIVE_MIN_AGE_MS (120') dal kickoff — sotto, il
     punteggio puo' ancora cambiare (fermate riprese il test che usava 1h).
  3) Le fonti esterne restano per i match ANCORA senza risultato sx-* (query
     diretta su match_results, tabella garantita da _create_results_table).
  Il percorso SX e' attivo SOLO se esiste almeno una fonte punteggi
  (ODDS_API_KEY o API_FOOTBALL_KEY) cosi' i test offline restano senza rete;
  disattivabile con `SX_NATIVE_SETTLEMENT=0`. Provider iniettabile
  (`settle_sx_bets(provider=...)`). Fail-safe: eccezioni catturate e
  loggate, mai chiuse righe senza punteggio reale.
  **Esito produzione 12/09 18:35 UTC**: bet #21 America MG–Nautico WON +2.25,
  #33 Jaguares–Fortaleza LOST -1.00, #34 Santa Fe–Tolima WON +1.46
  (netto +2.71 USDC, ZERO crediti the-odds-api). Restavano aperte solo le
  due del giorno: CSKA (si chiude coi punteggi live del percorso active)
  e Atalanta (kickoff 18:45, percorso normale). Test: 12 verdi in
  `test_sx_native_settlement.py` + autouse dummy provider in
  test_league_mapping (no rete) + regressioni settlement/bot/odds_api verdi.
  **Scoperta corollario**: the-odds-api NON copre Primera A/Nacional/K2
  (verificato su /v4/sports, 86 sport): NON aggiungerle a SPORTS_MAP — la
  copertura settlement di quelle leghe e' solo SX-native.

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

### Cambio strategia: SOLO CAMPIONATI VINCENTI (12/09/2026)

**Motivazione**: la strategia "solo favoriti 1.30-1.80" (11/09)
ha dato CLV vig-free **-3.3%** e circa 1 segnale/settimana
(molto poco). Il backtest storico rivela differenze massive tra
campionati:
- **Vince**: Bundesliga (+28.3%), PL (+16.2%), Turchia (+22.1%),
  Ligue 1 (+9.1%)
- **Perde**: Serie A (-5.9%), La Liga (-6.3%), Grecia (-69.4%)

**Implementazione** (value_filter.py):
- Fascia quote **1.50-2.20** (esclude il "pantano" 1.30-1.45
  con ROI -9.9%)
- `STRATEGY_LEAGUES`: solo campionati con ROI positivo
  (PL, Bundesliga, Turchia, Ligue 1, Eredivisie)
- Edge differenziato: PL/BL +2pp, Turchia/Ligue1 +2.5pp
- Kelly adattivo: PL 1.2x, BL 1.3x, altri base 1.0x
- Cap stake: PL/BL 2%, Turchia/Ligue1 1.8%, altri 0.5%
- Fallback severo per leghe non elencate (effectivamente bandite)

**Test**: `TestStrategiaPerLega` in `test_value_filter.py`
(10 test: leghe vincenti/perdenti/desconosciute, is_sane per
lega, get_league_strategy). Tripwire: nessun segnale da Serie A/
La Liga/Grecia.

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

**🔑 ROTAZIONE CHIAVE API-FOOTBALL (11/09 sera) — account ANCORA sospeso.**
Il proprietario ha resettato la chiave e l'ha incollata in chat: la chiave e'
stata considerata **compromessa** (regola 7) e va ruotata di nuovo appena la
sync funziona (Opzione A: lui la imposta da `railway variables --service api
--set`, l'agente verifica soltanto). La variabile e' stata aggiornata su
Railway con `railway variables --service api --set API_FOOTBALL_KEY=...`
(`API_FOOTBALL_KEY: preserve()` era gia' dichiarata in `.railway/railway.ts`,
quindi `railway config apply` non la distrugge) e il redeploy automatico ha
girato (deployment `afd45ab2-3338-4fd1-b703-7deea1122751`).
Verifica SENZA esporre la chiave: confronto dell'impronta sha256
(`len 32`, `sha256[0:12] = 5a6df8a8325e` identica a quella del valore atteso)
+ chiamata live a `/status`. Esito: **la chiave e' arrivata correttamente ma
l'account e' ancora sospeso** — `{'access': 'Your account is suspended,
check on https://dashboard.api-football.com.'}`, stesso errore dal container
e da una rete indipendente.  \nConseguenza: `--verify-ids` e la sync NON sono
stati lanciati (avrebbero dato 41/41 BAD bruciando quota per nulla). Il
reset della chiave NON rimuove la sospensione dell'account: va sbloccata dal
dashboard (o aprendo un ticket / con un account nuovo); tutto il resto e'
pronto e verificato, basta lanciare i due comandi quando l'API risponde.

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

**STATO CONTAINER — DEPLOY + REPAIR ESEGUITI (11/09 sera).**
Prima del push il container girava codice SENZA `SX_LEAGUE_ALIASES`,
`repair_sx_leagues`, `team_names` (verificato via `railway ssh`: attributi
assenti). Push su main `82aaa35..da9e88e` -> deploy automatico Railway
(deployment `8fc2711b-c126-4ddb-8239-d00a20779a14`), verificato con
`railway ssh ... python3 -c "import team_names"`.
Repair eseguito SUL container (`python3 sx_signals.py repair`):
**`{'checked': 118, 'updated': 17, 'inferred': 5}`** — 5 residui MLS
(`League One` -> `MLS`, inferiti dai roster) + 1 residuo belga
(`Premier League` -> `Belgian First Div`) + 11 righe corrette dall'etichetta
SX viva (`Liga Profesional` -> `Argentina Primera`, `Primera Division` ->
`Chile Primera`, `Primera Nacional`/`Primera A` = etichette SX grezze non
coperte, prima mappate per errore su `Primeira Liga`).
Verifica post-deploy sul container: kill-switch `effective: off`,
settlement in pausa, `ODDS_MIN/MAX 1.3/1.8` + favouriti only, cap
`1%/2%` con cap severo attivo, liquidita' `25/5/25/x2.0`, `team_names`
popolato. Ledger SX: 25 leghe distinte, **23/25 con sport key
the-odds-api** (restano fuori solo `Primera A` e `Primera Nacional`, non
coperte per scelta). `team_ratings` invariato a 200 (il repair non tocca i
rating). Health API 200.
⚠️ Crediti the-odds-api al momento del deploy: **58 residui** — con
settlement in pausa e kill-switch off il consumo e' solo la rotazione value
(~3,5/giorno).

**Verifica finale in locale (prima del deploy)**: 338 test verdi sul set
ampio + 130 sul set mirato (league_mapping, team_names, football_hist,
odds_api, rating_engine, poisson_engine, sx_signals, market_calib); tutti
gli id hanno roster; nessun roster vuoto in `ALL_LEAGUES`.
### Rotazione chiave API-Football + prima sync storica (12/09/2026)

**1) La chiave "nuova" era nel PROGETTO SBAGLIATO.** Esiste un **secondo
progetto Railway** (`valiant-liberation`) con un servizio chiamato
`quotaverace`: le modifiche fatte via RAW Editor e CLI finivano LÌ, non su
`quotaverace/api` (impronta `5a6df8a8325e` rimasta identica per 24h). Il
servizio orfano gira la stessa immagine ma crash-loopava su `ValueError:
Token non configurato.` perche' `config.py:53` legge `QUOTAVERACE_BOT_TOKEN`
mentre la' le variabili si chiamavano `TOKEN`/`BOT_TOKEN`/`TELEGRAM_TOKEN`
(mai lette → il crash-loop NON era il bot di produzione: lo scheduler di
`api` non si e' mai riavviato, ancoraggio `:25.416895` continuo).
⚠️ **Lezione operativa**: `railway list` prima di inseguire i log — con due
progetti attivi i log possono essere di un container estraneo.
Chiave valida trovata nel progetto orfano (`API_FOOTBALL_KEY` sha12
`fc8972c3a59e`; `/status` → `errors: []`, account "giuseppe bona", **piano
Free attivo fino al 12/09/2027**) e copiata su `quotaverace/api` con pipe
diretto fra i due progetti (valore MAI stampato in chat). Redeploy
`4d2dc56c` SUCCESS, container sano, nessun `ValueError`. **Progetto orfano
ELIMINATO** (`railway delete -p <id> --yes`, deletedAt programmato).
La chiave vecchia (`5a6df8a8325e`) e' morta: rigenerare la chiave invalida la
precedente (`Error/Missing application key`).

**2) Rate limit del piano Free: 10 richieste/MINUTO** (oltre alle
100/giorno). Sono limiti DIVERSI e il primo uccide i burst: `--verify-ids`
(41 chiamate di fila) ha dato **9/41 OK e 32 BAD** con `rateLimit: Too many
requests`. Non lanciare mai loop API-Football senza pacing.

**3) DUE BUG REALI trovati dalla sync** (silenziosi: `_league_response_ok`
scarta la lega e la sync importa 0 righe senza errore):
- `LEAGUE_API["La Liga"] = ("laliga", "spain")` ma l'API dice `La Liga` →
  **La Liga veniva saltata in OGNI sync**;
- `Turkey Super Lig` atteso `"super lig"` ma l'API dice `Süper Lig` (la
  dieresi rompe il contenimento); `Argentina Primera` atteso `"primera"` ma
  l'API dice `Liga Profesional Argentina`.
Corretti (le due leghe ora validano il solo PAESE). Tripwire:
`test_football_hist.TestNomiApiVerificati` (nomi reali API del 12/09).

**4) PACING del rate limit** (`football_hist._throttle`): ogni chiamata
`_api_get` (incluso il fallback di settlement in `sx_signals`) e' distanziata
di `API_FOOTBALL_MIN_INTERVAL` secondi (default **6.5** ≈ 9/min; `0` =
disattivato; environment dichiarata in `preserve()` in `.railway/railway.ts`).
Prima un burst bruciava richieste nei retry e faceva saltare le leghe. Test:
`test_football_hist.TestRateLimitThrottle` (default/env/attese).

**5) SYNC STORICA ESEGUITA (12/09, a lotti con pacing)**: 35 leghe,
**12.467 partite salvate**, zero leghe saltate (3 lotti: 5.791 + 3.933 +
2.743). `match_results` 2.725 → **15.192**; `team_ratings` 200 → **622
squadre su 40 leghe**; `sync_state` = **33 leghe marcate** (il job 08:30 non
le riscarica). Restano le 8 leghe core gia' profonde (Serie A, PL, La Liga,
Bundesliga, Ligue 1, Eredivisie, MLS, Brasileirao): le prende il job
automatico. Quota residua il 12/09: **31 richieste**.
⚠️ NB: il conteggio per lega in `team_ratings` ora e' "sporcato" dalle coppe
(una squadra che gioca in piu' competizioni prende la prima etichetta del
set: Brasileirao 18 → 2, Europa League 52): il gate usa il NOME SQUADRA, non
la lega, quindi nessuna regressione — le squadre rated sono triplicate.

### Settlement nativo SX + favoriti netti positivi nel backtest + scadenza righe stale (12/09 pomeriggio/sera)

**1) SETTLEMENT NATIVO SX (commit `0305ffc`, deployato e VERIFICATO in produzione).**
the-odds-api NON copre le leghe di alcune bet sx-* (Colombia Primera A,
Primera Nacional Argentina, K League 2: assenti dal catalogo /v4/sports —
verificato). Ma per le bet `sx-*` l'esito lo decide **SX stesso**: la fonte
corretta in assoluto per il nostro denaro e' il market risolto
sull'exchange, GRATIS (zero crediti). Implementato in `sx_signals.py`:
- `_results_from_sx(provider)`: (a) `markets/find` a BATCH di 30 sui
  `market_id` salvati sulle bet (bets.market_id = marketHash, li porta
  gia' dall'ordine) -> `outcome` 1|2 + `teamOneScore/teamTwoScore` per i
  mercati risolti; (b) fallback punteggi live su `markets/active` SOLO a
  >= 120' dal kickoff (prima: il punteggio live di un match in corso e'
  un verdetto sbagliato — test dedicato); (c) bet orfana senza riga in
  `matches` (es. bet #21 America MG–Nautico): ricostruita da zero
  (_same_event -> save_match -> save_result), la find ha anche rivelato
  la lega vera (Brasileiro Serie B). Semantica outcome: relativo alla
  gamba binaria del mercato (su "T1 vs Not T1": 1 = vince T1, 2 = non
  vince T1). Sempre `same_event`/`_norm_team` per l'aggancio, mai
  inversione casa/trasferta; guardia di unicita' come nel path esterno.
- **Gate attivazione**: il percorso SX-native segue le stesse regole delle
  altre fonti — attivo SOLO se `ODDS_API_KEY` o `API_FOOTBALL_KEY` sono
  configurate (i test offline restano network-free senza toccarli);
  disattivabile con `SX_NATIVE_SETTLEMENT=0`.
- Ordine fonti in `settle_sx_bets`: SX-native (gratis) -> the-odds-api
  (solo match ANCORA senza risultato sx-*, `missing`) -> API-Football
  (fallback). Poi `settle_bets` + `settle_predictions` (chiusura per
  match_id). Clamps per i test: `_sx_open_matches()` ora accetta
  `conn=None`, `_results_from_sx` usa `_create_results_table`.
- **Verificato IN PRODUZIONE** (job background post-deploy, 18:35 UTC):
  #21 America MG ✅ +2.25, #33 Jaguares ❌ −1.00, #34 Santa Fe ✅ +1.46
  → **netto +2.71 USDC, zero crediti**. #35 Atalanta e #36 CSKA si
  saldano da soli coi giri normali (erano in gioco al momento del check).
- Test: `test_sx_native_settlement.py` (18 test: find batch, live window,
  bet orfana, leghe non coperte, pausa, gate offline).

**2) ANALISI LEGHE — LA FASCIA FAVORITI E' POSITIVA NEL BACKTEST STORICO.**
Run walk-forward 2022–2026 (16.273 partite, `historical_backtest.py
--no-ou --max-odds 1.8 --save`, modello locale senza rating reali =
misura della sola macchina blend vs closing line reali): **215 bet,
ROI flat +5.9%, hit 60.5%, MaxDD 7.1%** (equity 100 → 112.7 senza mai
scavare). Tagli di robustezza (stake Kelly variabili -> P/L / STAKED):
- **Bucket quota (LA combinazione vincente): 1.60–1.80 = +21.9% ROI su
  55 bet (staked +18.5%), hit 65.5%** — e' il cuore del sistema.
  1.30–1.45 +7.4% (n=44), 1.45–1.60 **−9.9% (n=49, il pantano: solo
  12 con edge>=5pp)**, 1.60–1.80 +21.9% (n=55, 27 con edge>=5pp).
- Casa +9.0% (n=171) vs Trasferta −4.6% (n=44, p-value 0.08: non
  significativo ma orientato come la letteratura).
- La combo 1.60–1.80 ∩ edge>=5pp e' POSITIVA in TUTTE le 4 stagioni
  (n=25, +26.5% staked): consistente, non un anno fortunato.
- Confronto con lo stato AGENTS dell'11/09 ("produzione: 0 bet, il
  modello NON batte la closing line"): la differenza e' la FASCIA —
  quel run misurava l'universo lungo (fino a 5.0). Sui SOLO favoriti
  netti il gate e' positivo. ⚠️ Campione piccolo (215), staking Kelly
  1/4 cap 1%, niente look-ahead; e il gate live richiede edge >= 3pp
  (piu' selettivo del >= 5pp della combo migliore).
- Campiono reale sul container (liquidita', --with-model): 26 favoriti
  in fascia, 24/26 eseguibili (2 persi per profondita', −7.7%); edge
  positivi sul favorito 22/113 (p90 +12.5pp); 0 segnali value completi
  nel campione di 120 (la catena blend+fascia+EV+edge filtra tutto,
  come atteso con 5pp reali rari). CSV delle bet: data/
  historical_backtest_bets.csv (rigenerato col gate 1.30–1.80).
- **Non e' stato cambiato nessun gate di produzione** (solo evidenza
  di ricerca): eventuale restrizione futura della fascia live a
  1.60–1.80 = decisione del proprietario.

**3) SCADENZA RIGHE SX-* STALE (commit `d94bb6a`) — il buco crediti
chiuso.** Misurato sul container: 44 match sx-* aperti, 38 in finestra
(saldabili), **6 orfani fuori finestra da giorni** (batch 09/09 senza
riga in `matches`): a ogni giro entravano in `missing` e provocavano
`fetch_scores` (2 crediti) che NON li avrebbe mai trovati (la finestra
the-odds-api e' di 3 giorni). Nuovo `tracker.expire_stale_sx_rows()`:
- Riga SCADUTA = `match_id LIKE 'sx-%'`, `esito_finale IS NULL`,
  kickoff (commence_time in `matches`) piu' vecchio di `SX_STALE_DAYS`
  giorni (default **5**, letta a RUNTIME: env override senza redeploy)
  e NESSUN risultato in match_results → chiusa come **push (P/L 0)**:
  nessun verdetto inventato, solo lo stop ai costi.
- **Ramo orfani**: righe sx-* senza `matches` usano `created_at` come
  riferimento temporale (senza questo ramo le 6 righe del batch 09/09
  resterebbero aperte per sempre).
- Chiamata in CODA a `settle_sx_bets` (DOPO le fonti e i settle): se una
  fonte ha appena salvato il risultato, la riga e' gia' chiusa col
  verdetto vero — mai scadere una riga che una fonte stava per chiudere.
  Ritorno: campo `expired` = {bets, predictions} nel dict del settlement.
- Pausa settlement rispettata (0 righe scadute in pausa). Test: 18 in
  test_sx_native_settlement (incl. orfani scaduta/non-scade, pausa,
  SX_STALE_DAYS runtime).

**4) STATO OPERATIVO 12/09 sera.** Wallet 35.9978 USDC (exposure 2.0 =
le 2 bet del giorno), P/L live da ripartenza −3.13 USDC su 11 chiuse
(3 vinte, 8 perse — le perse quasi tutte pre-cambio-strategia a quota
3.0–4.35). Crediti the-odds-api ~450 con reset 01/10. Kill-switch live,
settlement attivo, STAKE_CAP_HARD=0 (floor 1 USDC su wallet 38).

### Riavvio bot + fix contatore crediti (12/09/2026)

**1) RIPRESA DELL'OPERATIVITA' (12/09, verificata sul container).**
- `data/execution/settlement_paused.json` -> `{"paused": false}` e
  `data/execution/auto_bet_mode.json` -> `{"mode": "live"}`: kill-switch
  `effective: live`, `provider_ready: true`, `_execution_mode()` = live,
  settlement RIATTIVATO. Nessun redeploy necessario (i job leggono i file a
  ogni giro).
- Primo giro di settlement reale: **72 partite aggiornate**, 17 previsioni
  saldate, 1 bet live (Venezia) saldata. Costo: ~52 crediti the-odds-api
  (26 leghe x ~2 crediti) -> **remaining 6** (reset 01/10). I 10 sx-* bets
  ancora aperti NON si saldano con `bot._update_results`: le bet con
  `match_id` sx-* passano da `sx_signals.settle_sx_bets()` (risultati
  salvati con l'id SX), che ha bisogno di credito per lega.
- Il bot gira e valuta i candidati: nei log `auto_bet: bankroll LIVE =
  saldo wallet 38.00 USDC` e **2 candidati favoriti ammessi dal gate**
  (uno strong su `sx-L19947936`) ma **0 ordini** per il CAP SEVERO
  (`stake 0.38/0.76 USDC < minimo ordine 1.00`). Con wallet 38 USDC il cap
  1%/2% non e' sostenibile: servono >= 100 USDC (1%) o >= 50 (2%), oppure
  `STAKE_CAP_HARD=0` per accettare il floor da 1 USDC (2.6% del bankroll).
  La decisione e' del proprietario: la configurazione attuale e' live ma
  senza ordini.

**2) BUG FIXATO — il contatore crediti era CIECO al consumo del settlement
(`odds_api.fetch_scores`).** `fetch_scores` salvava la cache punteggi come
`{"ts", "payload"}` SENZA `remaining`, mentre `_get_odds` lo salva: quindi
`get_remaining()`/`get_quota()` (minimo fra le cache `toa_*.json`) vedevano
solo il consumo della rotazione quote. Misura reale del 12/09: il contatore
diceva **58** mentre l'ultima risposta dell'API riportava **6** -> la guardia
proattiva `should_query_sport` NON ha ridotto le leghe e il credit watchdog
(`credit_watchdog_job`, soglie 50/20/10/5) non ha mai allertato: i crediti
sono stati bruciati dal settlement fino quasi a zero senza nessun campanello.
Fix: la cache punteggi registra anche `remaining` (con commento). Tripwire:
`test_odds_api.test_scores_cache_persiste_i_crediti` (fake response con
header `x-requests-remaining: 6` -> cache con `remaining`, `get_remaining()`
e `get_quota()` coerenti). Nota: le cache gia' scritte senza il campo restano
cieche finche' non vengono riscritte (un fetch le aggiorna).
`ODDS_DAILY_BUDGET` aggiunta in `preserve()` di `.railway/railway.ts`.

**3) Numeri utili del giro.** Wallet SX **37.9978 USDC** (exposure 0),
crediti the-odds-api **6** (reset 01/10), `team_ratings` **622** squadre,
`match_results` 15.192, 186 previsioni aperte su 26 leghe (23 mappabili; le
non mappabili sono scelte: `Primera A`, `Primera Nacional`, `K2-League`),
10 bet live aperte tutte pre-cambio-strategia (quote 2.09-4.35).

### Decisioni del proprietario 12/09/2026 (ripartenza degli ordini reali)

- **`STAKE_CAP_HARD=0`** (env su Railway, dichiarata in `preserve()`):
  scelta esplicita del proprietario per far ripartire gli ordini senza
  top-up. I cap PERCENTUALI restano 1% (value/moderate) e 2% (strong_value),
  ma ora il floor dell'exchange (1 USDC) prevale: su un wallet di 38 USDC
  ogni bet e' ~2.6% del bankroll. I risk cap di portafoglio sono intatti
  (correlazione 30% per blocco, esposizione totale 40%/giorno).
  ⚠️ Da rivedere se il wallet cresce (con >= 100 USDC il cap severo torna
  sostenibile e si puo' rimettere `STAKE_CAP_HARD=1`).
- **Prima bet reale della fase ripartita** (12/09 15:22 UTC): Atalanta vs
  Cagliari, esito `1` @ **1.75**, **1.00 USDC**, `FULLY_FILLED`
  (tx `0x0bea0e28…`). Wallet 37.9978 -> **36.9978** (exposure 1.0).
  Secondo candidato nella stessa tornata (CSKA Moscow vs Rubin Kazan,
  `sx-L19947936`, `1` @ 1.69): ordine **CANCELLED non riempito** -> nessuna
  riga a ledger e nessun ordine in eccesso (floor EV rispettato).
- **Crediti the-odds-api**: il proprietario crea una **chiave/account nuovi**
  (500 crediti freschi) e la imposta da se' (mai in chat, regola 7):
  `railway variables --service api --environment production --set-from-stdin ODDS_API_KEY`
  dalla root del repo (incolla il valore e Ctrl-D). Dopo il set: verificare
  la nuova impronta, poi lanciare il settlement delle righe rimaste aperte
  (10 bet live pre-cambio-strategia + ~186 previsioni) e riattivare la
  rotazione quote.

### Chiave the-odds-api nuova + settlement SX riparato (12/09/2026)

**1) CHIAVE NUOVA VALIDA.** Impronta `5c483976d988` (len 32), letta
  identica dal container dopo il redeploy. Verifica a costo ZERO sull'endpoint
  `/v4/sports` (non consuma crediti): **HTTP 200, `x-requests-remaining: 500`,
  used 0, 87 sport** → account nuovo, quota azzerata. Il fix del contatore
  crediti (deploy `f278844`) ora vede davvero il residuo.

**2) LE BET NON SI SALDAVANO: 3 cause, tutte misurate sul container.**
Con 500 crediti disponibili si poteva finalmente refertare, ma il giro
`_update_results` + `settle_sx_bets` scaricava 72 partite e chiudeva **0
righe**: i risultati c'erano (o erano a un passo) ma l'abbinamento falliva.
  1. **NOMI DIVERSI tra SX e the-odds-api** (la causa principale):
     `Cienciano` vs `Club Cienciano`, `CR Flamengo` vs `Flamengo-RJ`,
     `Vila Nova GO` vs `Vila Nova`, `Velez Sarsfield` vs `Velez Sarsfield BA`,
     `Corinthians SP` vs `Corinthians-SP`, `Newell's Old Boys` vs
     `Newells Old Boys`, `Goias` vs `Goiás`. `_norm_team`/`_loose_team` NON
     coprivano apostrofi, codici di stato (RJ/SP/GO/BA) e sigle diverse.
  2. **FINESTRA TROPPO CORTA**: `fetch_scores(sport, days_from=2)` escludeva
     le partite a >48h (es. Libertadores di due sere prima) — il massimo
     consentito dall'API e' **3** e il costo per chiamata NON cambia.
  3. **SOLO LE BET**: `_sx_open_matches()` guardava la tabella `bets`, quindi
     le previsioni SX dei match **senza puntata** restavano aperte per
     sempre (156 `rejected` + 6 value aperte): inquinavano la telemetria di
     calibrazione dei mercati.

**3) FIX (deploy verificato).**
  - `team_names.same_team(a, b)`: confronto **SIMMETRICO** tra due nomi di
     provider diversi (senza roster di verita'), a stadi deterministici —
     grezzo case-insensitive, `normalize`, `_core` (nuovo: toglie
     `REGION_CODES` RJ/SP/GO/BA/... e `JOIN_PARTICLES` de/del/do/da/los/...),
     poi contenimento di token. `normalize` ora **cancella l'apostrofo
     PRIMA della punteggiatura** ("Newell's" non diventa piu' `newell s`,
     che non agganciava 'Newells'). Mai fuzzy: `same_team` da' False per
     'Manchester United' vs 'Manchester City'.
  - `sx_signals._same_event()`: stadi `_norm_team` → `_loose_team` →
     `same_team`, **mai inversione casa/trasferta**.
  - **GUARDIA DI UNICITA'**: il confronto tollerante puo' agganciare piu'
     partite ("Manchester" sta in United e City). Se i candidati sono >1 la
     riga **RESTA APERTA** con warning: meglio un ritardo nel ledger che un
     verdetto col risultato di un'altra partita. (Il match tollerante senza
     guardia sarebbe stato un rischio di falsi positivi.)
  - `odds_api.SCORES_DAYS_FROM = 3` (default di `fetch_scores`) usato sia da
     `bot._update_results` sia dal settlement SX.
  - `_sx_open_matches()` = **UNION** bet + previsioni aperte (i match con
     sole previsioni ora ricevono il risultato) e `settle_sx_bets()` chiama
     anche `settle_predictions()`: in un giro chiude bet E previsioni.

**4) Test**: `test_team_names.TestSameTeam` (12 coppie reali parametrizzate +
  "mai fuzzy" + contenimento ambiguo documentato),
  `test_league_mapping.TestSettlementNomiTolleranti` (prefisso club, codici
  di stato + apostrofo, guardia di unicita', previsioni senza bet, finestra
  3gg), `test_settlement_source` aggiornato (import di `bot.py` su piu'
  righe). Focus verde: team_names + league_mapping + sx_signals +
  settlement_* + odds_api + scores_cache + bot + football_hist + rating/
  poisson engine.

**5) ⚠️ LIMITE NOTO — leghe non coperte da SPORTS_MAP.** Restano
  strutturalmente non saldabili le righe SX su **`Primera A` (Colombia)**,
  **`Primera Nacional` (Argentina)** e **`K2-League`**: il settlement le logga
  ("leghe non mappate... bet lasciate aperte") e NON brucia crediti. Per
  saldarle serve aggiungere la competizione a `SPORTS_MAP` (decisione di
  copertura/crediti, non un fix). Stesso limite per le righe **senza riga in
  `matches`** (21 previsioni + 1 bet del batch 09/09 23:03: senza nomi
  squadra non c'e' nulla da abbinare).
  ⚠️ **Verificare sempre che le soglie di liquidita' non blocchino il
  flusso**: la copertura rating e' cresciuta (622 squadre), quindi il gate
  modello e' ora misurabile.

**ESITO sul container (12/09, deploy `ce7823a0`).** Cache punteggi
  invalidate a mano (erano scritte col vecchio `days_from=2`), poi
  `_update_results()` + `settle_sx_bets()`:

| | prima | dopo |
|---|---|---|
| Bet live aperte | 10 | **5** |
| Bet saldate oggi | 3 | **8** |
| Previsioni aperte | 182 | **93** |
| Previsioni saldate oggi | 0 | **114** |
| Crediti the-odds-api | 500 | **452** |
| `match_results` aggiornati | — | 101 partite |

Le 5 bet ancora aperte: 2 sono di **Primera A (Colombia)**, 1 senza riga
  in `matches` (batch 09/09), 1 e' **CSKA–Rubin** (kickoff 17:45, in corso) e
  1 **Atalanta–Cagliari** (18:45) — le ultime due si saldano al giro
  successivo del job, senza intervento (la finestra 3gg le copre).

**Coda residua (13 match SX con kickoff passato ancora aperti)** — cause
distinte, tutte "non saldabili con le regole attuali", nessuna silenziosa:
  1. **Competizioni non coperte da `SPORTS_MAP`** (5 match + 2 bet):
     `Primera A` (Colombia), `Primera Nacional` (Argentina 2), `K2-League`
     (Korea 2). Serve una decisione di copertura/crediti: aggiungere la
     competizione a `SPORTS_MAP` + roster in `ALL_LEAGUES` + `LEAGUE_IDS`.
  2. **Etichetta di lega sbagliata nel ledger** (2 match): residui
     pre-fix-mapping (es. Millonarios–Deportivo Cali salvato come
     `Primeira Liga`, Union Berlin–Schalke come `Austrian Bundesliga`): il
     settlement interroga la competizione SBAGLIATA. `repair_sx_leagues()`
     e' conservativo e ha restituito `updated 0` (`{'checked': 127,
     'updated': 0, 'inferred': 0}`) — non indovina, quindi restano li'.
  3. **Payload API senza la partita** (1 match): la Champions del 10/09 non
     compare in `scores` (`soccer_uefa_champs_league` ha risposto SOLO con
     le partite future di ottobre) — limite del piano/API, non del codice.
  4. **Varianti di nome non coperte in modo deterministico** (3 match):
     `BB Erzurumspor` vs `Erzurum BB`, `FC Kopenhagen` vs `FC Copenhagen`,
     `FC Red Bull Salzburg` vs `RB Salzburg`. Coprirle richiede uno stadio
     di similarita' (rischioso: falso positivo = verdetto di un'altra
     partita) → possibile in futuro SOLO con threshold alta + guardia di
     unicita' gia' presente.
  5. **Senza riga in `matches`** (7 match / 21 previsioni + bet #21): niente
     nomi squadra, quindi niente da abbinare (recuperabili solo se il
     mercato SX e' ancora attivo e leggibile).
  6. **Future o in corso**: ~22 match (12-13/09) — correttamente aperti.

**BONUS fix contatore crediti (12/09, `get_remaining`).** Con la chiave nuova
  l'API riportava **452** crediti ma `/api/health` e la guardia proattiva
  continuavano a leggere **58**: `get_remaining()` prendeva il **MINIMO** tra
  tutte le cache `toa_*.json`, e le cache quote scritte con la chiave VECCHIA
  tengono quel valore per 3-30 giorni (si rinnovano per lega). Ora vale la
  lettura **piu' recente**: nuovo campo `remaining_ts` scritto da
  `_get_odds`/`fetch_scores` (in `fetch_scores` il `ts` della cache puo'
  essere preservato da un giro precedente, `remaining_ts` e' sempre il
  momento della chiamata), con ripiego su `ts` per le cache di formato
  vecchio. Stessa helper (`_latest_credits`) anche per `get_quota()`: la
  `/api/health` mostrava il valore stantio perche' duplicava il minimo.
  Verificato post-deploy: `get_quota()` = `(452, 89)` e health
  `"remaining": 452`. Tripwire:
  `test_get_remaining_usa_la_lettura_piu_recente`,
  `test_get_remaining_senza_remaining_ts_usa_ts`.

### Audit completo + 502 in produzione + guardrail ripristinati (13/09/2026)

**1) PRODUZIONE GIU' (502): conflitto di merge COMMITTATO.** `origin/main`
  conteneva le righe `<<<<<<< / ======= / >>>>>>>` dentro `auto_bet.py`
  (commit `2c42baa`, dalla serie "FREQUENZA boost" `40deffb`/`742bb9f`/
  `2c42baa`): il container crashava all'import → **502 su tutto** (bot fermo,
  nessun settlement, nessuna puntata) e nessuna notifica Telegram (il bot e'
  morto col container). `origin/main` differiva dal commit locale **SOLO** per
  quelle 4 righe. Fix: merge risolto (commit `81f2bff`), con verifica che
  l'albero finale sia IDENTICO al fix locale (`git diff --stat <fix> HEAD`
  vuoto — il primo merge aveva ripreso il test vecchio, corretto con
  `git checkout <fix> -- test_favourites_only.py` + `--amend`).
  **Regola permanente (nuova regola 8):** prima di ogni push,
  `grep -rn "^<<<<<<<" *.py` deve essere vuoto.

**2) BUG REALE nel commit "FREQUENZA boost": `dynamic_kelly_stake` scalava
  l'EV DUE VOLTE** (`frac = dynamic_kelly(ev, ...)` e poi
  `ev_factor = ev/0.20`), quindi lo stake finiva **sempre** sul floor
  dell'exchange: con wallet 38 USDC 0.07–0.35 → **1.00 USDC = 2.63% del
  bankroll**, cioe' il CAP SEVERO 1%/2% e il fail-closed di `STAKE_CAP_HARD`
  erano **di fatto disattivati** (fail-open silenzioso). Misura: bankroll 38 →
  1.00; 100 → 1.00; 500 → 1.00–14.53 (solo con bankroll grande il floor
  smetteva di mordere). **Ripristinato l'adaptive staking** (`adaptive_stake`,
  cap severo rispettato), conservando timing filter (`get_optimal_timing`),
  odds movement bonus (+20% stake su quota a -5%) ed exposure control
  (`calculate_exposure`). Rimossa `dynamic_kelly_stake`;
  `value_filter.dynamic_kelly` resta esportata (retrocompatibilita' test).

**3) GUARDRAIL 11/09 RIPRISTINATI.** Il commit "FREQUENZA boost" aveva
  rilassato le soglie **e anche le asserzioni del tripwire che le
  proteggevano** (`assert ODDS_MAX <= 2.00`, `MARKET_EDGE_MIN >= 0.02` — col
  docstring che continuava a dichiarare "1.30-1.80 / +3pp": il tripwire non
  proteggeva piu' nulla). Ripristinati i valori della direttiva prudente:
  - `ODDS_MAX` 2.00 → **1.80** (con `ODDS_MIN` 1.30 = fascia favoriti netti)
  - `MARKET_EDGE_MIN` 0.02 → **0.03** (+3pp), `MARKET_EDGE_MODERATE` → **0.03**
  - `MARKET_EDGE_STRONG` 0.04 → **0.05** (+5pp strong_value)
  - `DEFAULT_LEAGUE_STRATEGY.min_edge` 0.02 → **0.03**
  Tripwire riallineato: `test_risk_guards.TestFasciaFavoriti` asserisce
  `ODDS_MAX == 1.80`, `MARKET_EDGE_MIN >= 0.03`, `MARKET_EDGE_STRONG >= 0.05`;
  `test_edge_sotto_3pp_bocciato` (+1pp e +2pp bocciati, +3pp passa);
  `test_value_filter` (`ODDS_MAX == 1.80`, `min_edge fallback 0.03`,
  `test_quota_2_00_bocciata`). La webapp (`webapp/app/value/page.tsx`) diceva
  gia' "1.30-1.80 / +3pp": ora e' di nuovo vero. ⚠️ Conseguenza ATTESA:
  **meno segnali** (gate piu' selettivo) — e' la scelta prudente, non un bug.

**4) ALTRI FIX.** Testi dei filtri Telegram derivati dalle costanti reali
  (`bot.FILTRI_TXT` → "EV 2%-20% | Odds 1.30-1.80 | Edge ≥ +3pp | Kelly
  frazionato | Cap 0.5-2%") invece che hardcoded; `historical_backtest._kelly_stake`
  importava `KELLY_FRACTION` da `value_filter` (inesistente → ImportError),
  ora usa la frazione locale.

**5) VERIFICA FINALE (13/09).** Suite completa **1050 test verdi** su 63 file
  (in lotti: la suite intera supera il timeout di 10' della shell — NON
  utilizzabile in background, i processi figli vengono terminati), 0 marker di
  conflitto, `compileall` OK. Produzione: `/api/health` **200**, scheduler +
  tutti i job registrati, ensemble retrain **n=219** (Brier 0.0448, acc 0.982),
  calibrazione isotonica **65 campioni OOF** (0.2691 → 0.2145), backup
  integrity ok, kill-switch `effective: live` + `provider_ready: true`,
  settlement NON in pausa, wallet SX **35.98 USDC**. Ledger: 38 bet (2 aperte,
  entrambe live), 107 previsioni aperte, `match_results` 15.378,
  `team_ratings` 661, `matches` 316. Contatore crediti **410**
  (`get_remaining()`; `/api/credits` mostra ancora `remaining_min` 58 =
  minimo prudente per design, non un residuo del bug). Il log auto_bet ora
  recita "adaptive Kelly" (prima "dynamic Kelly").

### Verifica settlement + orfani + copertura leghe (13/09/2026, sera)

**1) ESITO DEL SETTLEMENT IN PRODUZIONE (giro manuale + automatico).**
  Previsioni aperte **107 → 92**, bet aperte **2 → 1** (l'unica rimasta e' una
  live di Serie A con kickoff posticipato), 139 risultati scaricati (39 nuovi
  in `match_results`), costo **24 crediti** (410 → 386). Il settlement
  automatico gira: `sx_signals_job` ogni 15' (percorso SX-native, gratis) +
  watchdog. **0 delle 92 righe aperte ha un risultato in `match_results`**:
  nessun disallineamento, il residuo e' tutto spiegato — 21 righe orfane +
  1 OU senza riga in `matches`, ~34 in leghe fuori dal catalogo
  the-odds-api, il resto partite future o oltre la finestra di 3 giorni.
  ⚠️ Nota operativa: `sx_signals_job` puo' durare ~4-5 minuti e in quella
  finestra fa scattare `auto_bet_job skipped: max running instances (1)`
  (anti-sovrapposizione voluta; il giro si riprende al minuto dopo).

**2) RIGHE ORFANE: NON RECUPERABILI, E ORA SCADONO TUTTE.** Le 21 righe
  `sx-*` senza riga in `matches` (batch 09/09 e 10/09) **non hanno market_hash
  sul ledger** (nessuna bet associata) ne' nomi squadra: `markets/find`
  richiede gli hash e `markets/active` non le elenca piu' (eventi conclusi),
  quindi **nessuna fonte puo' piu' saldarle**. Erano gia' coperte dal ramo
  orfani di `expire_stale_sx_rows` (chiusura come push dopo `SX_STALE_DAYS`).
  **Estensione 13/09**: il ramo orfani non e' piu' limitato a `sx-%` —
  senza riga in `matches` mancano kickoff E nomi, quindi la riga e'
  insaldabile con QUALSIASI prefisso (caso reale: la previsione OU
  `6185eb4f...` del 01/09, rimasta senza partita e aperta da 12 giorni).
  Le righe CON riga in `matches` restano intatte (refertabili, le chiude il
  settle vero): test dedicati `test_orfana_non_sx_senza_matches_scade` e
  `test_orfana_non_sx_con_match_non_scade` in test_sx_native_settlement.py.

**3) COPERTURA LEGHE SCOPERte — VERIFICATO: NON AGGIUNGIBILI.** Chiamata
  REALE a `/v4/sports` (costo **0 crediti**: l'endpoint non consuma quota,
  remaining invariato a 386): 84 sport totali, 49 di calcio. **Colombia:
  ASSENTE**; **Korea: solo `soccer_korea_kleague1`** (nessuna K League 2);
  **Argentina: solo `soccer_argentina_primera_division`** (nessuna Nacional).
  Quindi `Primera A`, `Primera Nacional`, `K2-League` non sono copribili —
  non e' una scelta di crediti, la competizione non esiste nel catalogo.
  Confermata la nota 12/09: **NON aggiungerle a `SPORTS_MAP`**. Le altre
  label non mappate (`Division Profesional`, `LigaPro`, `First League`)
  sono ambigue senza corrispondenza univoca (il resolver torna None per
  prudenza: mai indovinare la competizione). Per queste leghe l'unico
  percorso di settlement e' SX-native, e funziona solo per le righe con
  market_hash sul ledger.

**4) ETICHETTA LOG CORRETTA.** `auto_bet` stampava "strategia FREQUENZA
  alta" (residuo del commit 13/09): ora dice "strategia favoriti netti
  (EV_MIN=2%, ODDS 1.30-1.80, edge >= +3pp, adaptive Kelly)", con i valori
  presi dalle costanti reali — cosi' un operatore che legge i log non puo'
  credere attiva una strategia che non c'e' piu'.

### Costo reale del settlement + diagnosi del residuo (13/09/2026, notte)

**1) MISURATO: il settlement costava ~28 leghe/giorno, di cui META' per pura
  verifica.** Un giro manuale reale e' costato **24 crediti** (410 → 386) —
  valore in eccesso, perche' nella stessa finestra giravano anche gli altri
  job. Il pianificatore sul DB di produzione (13/09) dice: **28 leghe
  pianificate**, di cui **14 con righe aperte** (7 delle quali NON mappate →
  saltate a costo 0) e **14 che entravano SOLO per la finestra di
  verifica/heal** (tutte le grandi: PL, Serie A, La Liga, Bundesliga,
  Ligue 1, Eredivisie, Brasileirao, CL, EL, Championship, Serie B,
  Allsvenskan, Belgio, Brazil B). Poiche' la cache punteggi ha TTL 24h,
  quelle 14 venivano riscaricate ogni giorno per ri-verificare righe GIA'
  CHIUSE: era la voce di costo principale. Le leghe NON mappate
  (`Primera A`, `Nacional`, `K2-League`, `Division Profesional`, `LigaPro`,
  `First League`) erano gia' saltate PRIMA della chiamata → costo 0,
  nessuno spreco da tagliare li'.

**2) DUE LEVE DI RISPARMIO (env, zero redeploy di codice).**
  - **Finestra di refertazione allineata alla API** (`SETTLEMENT_WINDOW_DAYS`,
    default `3` = `odds_api.SCORES_DAYS_FROM`): il pianificatore usava **5**
    giorni mentre `/scores` copre **3**, quindi una lega le cui uniche righe
    aperte stavano a 3-5 giorni veniva interrogata (PAGATA) senza poter
    saldare nulla. Ora `_settlement_window_days()` li tiene allineati e un
    tripwire lo verifica.
  - **Verifica periodica invece che a ogni scadenza cache**
    (`SETTLEMENT_HEAL_INTERVAL_HOURS`, default **36**): una lega SENZA righe
    aperte viene ri-interrogata solo se la sua cache punteggi e' piu'
    vecchia dell'intervallo (prima: ogni 24h, cioe' ogni giorno). Con 0 si
    torna al comportamento pre-13/09. Le leghe **con righe aperte si
    interrogano sempre**, con cache fresca o no: il risultato serve per
    saldare.
  Sui numeri del 13/09: la popolazione di verifica passa da 1 fetch/24h a
  1/36h (-33% su 14 leghe) → costo atteso da ~21 a ~16 crediti/giorno.

**3) IL COSTO ORA E' MISURABILE E LEGGIBILE A COLPO D'OCCHIO.**
  - **Log per giro** (`bot._update_results`): `settlement: N leghe
    interrogate (M non mappate, saltate), K partite aggiornate, crediti
    X -> Y (D usati)` — prima il consumo del settlement non era misurato da
    nessuna parte (si leggeva solo il contatore globale).
  - **`tracker.settlement_residue()`** rompe le righe aperte per MOTIVO:
    `no_match_row` (insaldabile: niente kickoff ne' nomi), `league_unmapped`
    (fuori catalogo/etichetta ambigua), `out_of_window` (partita piu' vecchia
    della finestra /scores), `not_started` (futura), `awaiting_result`
    (refertabile, risultato non ancora arrivato). Aggiunge il costo atteso
    del prossimo giro (`estimated_credits`, conta solo le leghe mappate con
    cache scaduta), lo split `cost_open_driven` / `cost_heal_only`, il
    pianificatore (`leagues_to_query`) e **`overdue_orphans`**.
  - **Esposto in `GET /api/health`** (campo `settlement`): il residuo non e'
    piu' un numero opaco.
  - **`overdue_orphans` DEVE essere 0**: sono righe insaldabili oltre la
    soglia di scadenza. Se sale, la scadenza automatica (`expire_stale_sx_rows`)
    non sta girando. **E' il controllo automatico dei 21 orfani `sx-*`**: la
    verifica "dopo il 15/09" e' che questo campo resti **0** e che le righe
    del batch 09/09 (18 righe, scadenza 14/09 23:03 UTC) e 10/09 (3 righe,
    15/09 18:30 UTC) non siano piu' aperte.
  Test: `test_settlement_watchdog.py` (`TestRisparmioCreditiSettlement`,
  `TestResiduoSettlement` — allineamento finestra, controprova che la vecchia
  finestra a 5gg le interrogava, verifica periodica vs cache fresca, lega con
  righe aperte sempre interrogata, classificazione dei motivi, costo atteso
  con cache fresca/scaduta, residuo vuoto).

### Workflow agentico di ricerca su grafo (14/09/2026, `research_graph/`)

Modulo NUOVO e INDIPENDENTE (non tocca tracker/bot/produzione, zero rete):
workflow di ricerca a grafo con validazione IBRIDA (deterministica + semantica)
e ciclo di retry con feedback strutturato, cap a **3 attempt**. Nasce come
infrastruttura riutilizzabile per la ricerca (strategie 2026, CLV, devigging).

- **Struttura**: `models.py` (schema Pydantic + stato), `providers.py`
  (contratti `SearchTool`/`Validator` + Mock), `nodes.py` (i 4 nodi + terminali),
  `graph.py` (mini StateGraph + wiring), `__main__.py` (demo con i mock).
  Dipendenza nuova: **pydantic >= 2.0** (aggiunta a requirements.txt).
- **Stato centrale** (`ResearchState`): `findings` (validati, cumulativi),
  `raw_findings` (buffer del SOLO attempt corrente), `validation_feedback`,
  `attempt`, piu' `attempts_log`, `node_trace`, `rejected`.
- **Research Agent**: 1o giro = query generale; sui retry legge il
  `validation_feedback` e genera query MIRATE (`queries_for_retry`: query
  suggerite -> lacune -> problemi -> fallback deterministico), mai duplicati
  (`state.add_query`). Errori del search tool catturati (fail-safe).
- **Controlli deterministici (Pydantic)**: valida il buffer dell'attempt con
  `Finding.model_validate` (campi obbligatori, confidence in [0,1], evidenza
  >= 20 char), dedup su (claim, fonte) e minimo `MIN_FINDINGS` (2). Un payload
  non conforme NON arriva mai all'LLM e produce un fail con indici/campi
  respinti. I respinti non vengono ri-validati (un finding malformato non
  avvelena i retry) e un retry senza query nuove non azzera il materiale gia'
  raccolto.
- **LLM Validator (semantico)**: riceve `ValidationRequest` (query, claim
  richiesti, findings validati, attempt) e risponde `pass`/`fail` + feedback;
  le uscite `dict`/stringa sono normalizzate con Pydantic (`coerce_verdict`),
  un errore dell'adapter diventa un fail con feedback (fail-closed).
- **Decision node**: `pass` -> finalize, `fail` -> retry, `fail` con
  `attempt >= max_attempts` -> **hard_stop** (il cap sta nel router, non nei
  nodi). Il verdetto `fail` SENZA feedback e' rifiutato alla costruzione:
  il retry non puo' restare senza istruzioni.
- **Engine**: `StateGraph` con edge semplici/condizionali, validazione del
  wiring in `compile()` (nodo senza uscite, target inesistente, uscita
  doppia) e tetto di nodi eseguiti (`max_steps`): un ciclo non protetto
  SOLLEVA `GraphError` invece di girare. `run_research` e' fail-safe (errori
  imprevisti -> `status="error"`, mai eccezioni al chiamante).
- **Mock-first**: `MockSearchTool` (dict/callable/batch a copione, registra
  `calls`) e `MockLLMValidator` (copione di verdetti, `requests`, default per
  i casi ripetuti). Gli adapter reali si sostituiscono in
  `build_research_graph(search=..., validator=...)` **senza toccare nodi ed
  edge** (verificato da un test con adapter custom).
- **Test** (`test_research_graph.py`, 40 verdi): i tre scenari richiesti
  (stop immediato al 1o attempt; retry con feedback che guida query mirate e
  passa al 2o; hard stop a 3 attempt con validator chiamato esattamente 3
  volte) + schema Pydantic, dedup, retry senza query nuove, agnosticismo
  provider, guardie dell'engine e fail-safe.
- Demo: `venv/bin/python -m research_graph [retry|hard-stop|schema|all]`
  (tutto sui mock: nessuna rete, nessun credito, nessun ordine).

**Passo 1 — adapter di ricerca REALE (Exa)** (default `type: "neural"` dal
14/09: e' l'unico tipo che restituisce `score`, vedi sotto) (`research_graph/exa_search.py`).
Il contratto `SearchTool` implementato con ricerca web vera, zero dipendenze
nuove (`requests` e' gia' nel progetto): `POST https://api.exa.ai/search`,
auth `Bearer` con `EXA_API_KEY`, `contents: {highlights, text}`. Scelto con la
Gravity Index perche' e' un motore pensato per agenti (risultati citabili;
`highlights` = estratti dei soli token rilevanti, il materiale ideale per
`Finding.evidence`). Mapping: `claim <- title`, `evidence <- highlights`
(fallback testo troncato), `source <- url`, `confidence <- score` se in [0,1]
altrimenti 0.5. FAIL-CLOSED senza chiave (`ExaSearchError`, nessuna ricerca
inventata), errori HTTP/JSON -> eccezione (il nodo la registra, il cap degli
attempt fa da rete), risposta senza `results` -> lista vuota (nessuna evidenza:
fail deterministico -> retry mirato). I risultati senza titolo ne' url vengono
scartati. Chiave SOLO da env o iniettata; `http_post` iniettabile -> test
OFFLINE (trasporto finto). Verifica manuale reale:
`venv/bin/python -m research_graph live "<query>"` (richiede `EXA_API_KEY`).

**Passo 2 — persistenza del trace** (`research_graph/trace_store.py`).
Ogni run (passato, hard stop o errore) lascia una riga JSONL append-only sul
volume (`RESEARCH_TRACE_DIR`, default `DATA_DIR/research`): query, claim
richiesti, status, attempt, findings/respinti, query usate, feedback di ogni
giro, node_trace. `run_research(..., trace_store=TraceStore(path))` — la
persistenza sta FUORI dal grafo (il workflow non cambia) ed e' FAIL-SAFE: una
scrittura fallita finisce in `record["error"]` e non rompe mai la ricerca.
Lettura/riepilogo: `iter_traces` (piu' recenti prima, righe corrotte ignorate,
limite/finestra), `summary` (run, pass rate, attempt medi, tipi di feedback,
top blocker), `format_report` (Telegram-friendly). CLI:
`venv/bin/python -m research_graph.trace_store [--limit N] [--days N] [--json]`.
La demo supporta `--trace [--trace-path PATH]`.

**Passo 3 — adapter LLM REALE (Gemini)** (`research_graph/gemini_validator.py`).
Il contratto `Validator` implementato con `google-genai` (gia' nel progetto,
pattern di `ai_commander.py`), JSON mode e `GOOGLE_API_KEY` SOLO da env.
`build_prompt` e' deterministico (query, claim numerati, ogni evidenza con
fonte e confidenza) cosi' e' ispezionabile e testabile senza rete; il parsing
(`extract_json` + `verdict_from_data`) e' difensivo: accetta code fence e testo
attorno, schema piatto o annidato, normalizza i campi; una risposta
ILLEGGIBILE non diventa mai un pass ma un fail con feedback (fail-closed),
mentre un errore di trasporto solleva e il nodo lo converte in fail. Client
iniettabile -> test offline con client finto; chiave mai nel prompt (tripwire
dedicato). `RESEARCH_LLM_MODEL` per cambiare modello (default
`gemini-3.6-flash`, come `ai_commander`).

**Test**: `test_research_graph.py` (42), `test_research_exa.py` (22),
`test_research_trace.py` (17), `test_research_gemini.py` (26) — **107 verdi
offline** (`-m "not integration"`) e **109 verdi** con i due `integration`
attivi (rete + quota, si accendono da soli quando `EXA_API_KEY` e
`GOOGLE_API_KEY` sono configurate). Nessuna chiave in chiaro nei sorgenti
(il tripwire `test_secret_hygiene.py` resta verde: le credenziali finte dei
test sono marcate `fake/`).

**Chiavi reali in esercizio (14/09/2026)** — gli adapter vedono davvero
`EXA_API_KEY` e `GOOGLE_API_KEY`: verifica LIVE end-to-end
`venv/bin/python -m research_graph live "<query>"` -> 5 pagine Exa, Gemini
reale, verdetto `pass` al 1o attempt, `pagine raccolte: 5`; i due test
`integration` passano (`pytest -m integration`: 2 verdi).
⚠️ Due trappole verificate sul campo (da non ripetere):
1) il progetto legge SOLO il `.env` nella ROOT del progetto
   (`config.load_dotenv` usa il path del modulo, NON `find_dotenv`): una
   chiave scritta in `$HOME/.env` NON viene mai letta — la variabile risulta
   assente e l'adapter resta fail-closed. Verifica rapida senza stampare
   valori: `exa_configured()` / `gemini_configured()`.
2) `secrets_store.py vault --commit` NON fa merge: ricostruisce `vault.bin`
   dai SOLI file plaintext presenti in `secrets/`, quindi CANCELLA i segreti
   gia' cifrati (qui ne avrebbe distrutti 3 su 4). Per aggiungere un segreto:
   merge esplicito (`load_vault()` -> aggiungi la voce -> risecrittura atomica
   con `_fernet_from_master(_master_key())`, chmod 600) oppure mettere in
   `secrets/` i file plaintext di TUTTE le voci prima del commit. `EXA_API_KEY`
   e' entrata con il merge: il vault ora ha 5 segreti (API_FOOTBALL_KEY,
   EXA_API_KEY, GITHUB_TOKEN, GOOGLE_API_KEY, QUOTAVERACE_BOT_TOKEN).
   Backup del vault pre-modifica lasciato in `/tmp` (mai in `secrets/`: i
   file non in `_SKIP_NAMES` verrebbero letti come plaintext e finirebbero in
   `os.environ`).
Con le chiavi presenti i due `integration` NON si saltano piu' nella suite
completa (usano rete e quota): per un giro offline `-m "not integration"`.

**Exa: default `neural` per avere confidence reali (14/09).** Il campo `score`
(mappato su `Finding.confidence`) arriva SOLO con `type: "neural"`: col tipo
`auto` il payload non lo contiene (verificato chiamando l'API: i campi del
risultato sono favicon/highlights/id/image/publishedDate/text/title/url) e la
confidence restava sempre `DEFAULT_CONFIDENCE` (0.5). Ora il default e'
`neural` (`DEFAULT_SEARCH_TYPE`, override con l'env `EXA_SEARCH_TYPE`; nuovo
`ExaSearchTool.resolved_search_type()`), e nella verifica live le confidence
sono reali: 1.00 / 0.75 / 0.50 / 0.25 / 0.00 (Exa normalizza lo score sul set
di risultati: l'ultimo puo' valere 0.0, ed e' accettato da `Finding`, che
richiede solo 0 <= confidence <= 1). Per tornare al vecchio comportamento
senza score: `search_type="auto"` (o `EXA_SEARCH_TYPE=auto`); con
`search_type=""` il campo `type` viene omesso. Test: classe
`TestTipoDiRicerca` in `test_research_exa.py` (default neural, env che cambia
il default, esplicito che vince sull'env, tipo vuoto, score -> confidence).

**Chiavi in produzione su Railway (14/09).** `EXA_API_KEY` e `GOOGLE_API_KEY`
sono state impostate sul servizio `api` (ambiente production), lette dal VAULT
e passate via stdin (`venv/bin/python secrets_store.py get NOME | railway
variable set NOME --stdin --service api`): il valore non e' mai transitato in
chat. Verificato SUL CONTAINER (dopo redeploy SUCCESS): `EXA_API_KEY` len 36
sha12 `77620c539824`, `GOOGLE_API_KEY` len 53 sha12 `23099b012e46` (le stesse
impronte di vault e locale), `/api/health` 200. Le due voci sono dichiarate in
`.railway/railway.ts` (`researchEnv`, SOLO sul servizio `api`: il cron surebet
non ne ha bisogno) insieme a `EXA_SEARCH_TYPE`, `RESEARCH_LLM_MODEL`,
`RESEARCH_TRACE_DIR`, cosi' `railway config apply` non le distrugge.
⚠️ Da sapere:
- `research_graph/` NON e' ancora deployato (i moduli sono uncommitted):
  sul container le env ci sono ma `import research_graph` da'
  `ModuleNotFoundError` finche' il codice non viene push-ato su `main`.
- C'erano DUE chiavi Google diverse (len 53 entrambe): la shell/`~/.env`
  (`23099b012e46`) vinceva sulla vault (`f03990738182`) perche' `config` usa
  `os.environ.setdefault`. Entrambe valide (verificate con una chiamata
  Gemini reale), ma la doppia fonte era ambigua: il vault e' stato allineato
  a `~/.env` e Railway usa ora quella, quindi vault = `~/.env` = Railway.
- La riga `EXA_API_KEY` duplicata in `$HOME/.env` e' stata rimossa (backup in
  `/tmp/home-env.bak-*`): il progetto la legge dal vault.
- `railway config plan` FUNZIONA: la CLI 5.54.1 valuta il `.railway/railway.ts`
  con l'SDK `railway@3.11.0` di `/home/siryo/node_modules`, che e' esattamente
  l'ultima versione pubblicata (`npm view railway version` → 3.11.0). Nessun
  pacchetto obsoleto da sistemare.
  ⚠️ NON avvolgere il comando in un wrapper (`timeout 120 railway config plan`):
  il check di compatibilita' dell'SDK (`assertMinimumIacCliVersion`) esegue
  `$process.env._ --version` e pretende una terna x.y.z ≥ 5.42.1. Con `$_` =
  `timeout` (`timeout (GNU coreutils) 8.32`) il check fallisce con
  "This version of railway/iac requires Railway CLI 5.42.1 or newer": e' un
  FALSO allarme, non c'entra col file IaC. Lanciare il comando nudo.

**Drift IaC chiusa (14/09).** `railway config plan` segnalava 2 variabili
DISTRUTTIVE (presenti su Railway ma non dichiarate nel file):
`api.TENNIS_SANDBOX_ENABLED` — un `config apply` avrebbe SPENTO il sandbox
tennis (scan+settle) — e `surebet.SUREBET_CRON_HOLD_SECONDS`; in piu'
`surebet deploy.restartPolicyType` ("NEVER" → null: un apply avrebbe rimesso il
restart su errore su un container-cron che DEVE uscire a fine scan). Corretto
dichiarando con `preserve()` le variabili (`TENNIS_SANDBOX_ENABLED` piu' i limiti
di staking tennis, `SUREBET_CRON_HOLD_SECONDS` solo sul cron) e
`restartPolicyType: "NEVER"` nel blocco `deploy` del cron. Ora il piano e'
**"0 to add, 1 to change, 0 to destroy"** (l'unico cambio e' il flag
`config.isCreated` di api-volume, non distruttivo e gia' dichiarato nel file).
Regola: dopo ogni modifica al file IaC o alle env da dashboard, girare
`railway config plan` e pretendere **0 to destroy**.

### Catena di decisione `decision/` (14/09/2026)

Direttiva del proprietario: dare alla pipeline una struttura esplicita
Signal → Risk → Stake, con kill switch come autorita' superiore e un feedback
engine che registra tutto. **Decisioni prese**: (1) nuovo pacchetto `decision/`
SOPRA i moduli esistenti (niente riscrittura di `auto_bet`); (2) il verdetto
`review` = coda + approvazione umana su Telegram; (3) precedenza blocchi:
**kill switch manuale > stop-loss giornaliero > pausa settlement**.

**Perche' un orchestratore e non un motore in piu'**: la catena riprende
moduli che esistono gia' (Signal = `sx_signals`/`fixture_engine` + `value_filter`
e `market_calib`; Risk = i gate + i cap di `auto_bet`; Stake =
`adaptive_staking`; Kill switch = `auto_bet_mode.json`/`daily_stop.json`/
`settlement_paused`; Feedback = ledger `tracker` + `ml_ensemble` + `drift_monitor`),
ma **ogni stadio ha un contratto** e le soglie vivono in un solo posto.

**File**:
- `decision/models.py` — contratti Pydantic: `Signal` (con `DataQuality`),
  `RiskDecision`, `StakeDecision`, `KillSwitchStatus`, `DecisionRecord`
  (`as_row()` = riga piatta per il feedback engine) e `ReasonCode`
  (motivi machine-readable, mai prosa). `KILL_SWITCH_PRECEDENCE` e' la
  precedenza decisa dal proprietario.
- `decision/limits.py` — `RiskLimits.from_env()`: **nessun default copiato a
  mano**, i valori si LEGGONO da `value_filter`/`market_calib`/
  `adaptive_staking` (e dagli stessi env di produzione). `required_depth()`
  riproduce max(stake x 2.0, 25 USDC).
- `decision/kill_switch.py` — istantanea dei blocchi con sonde iniettabili.
  Fail-safe in direzioni OPPOSTE e volute: modalita' illeggibile -> `off`
  (fail-closed), stop-loss illeggibile -> non attivo (fail-open). La pausa
  settlement NON blocca la bet: e' un `advisory` (blocca il referto/feedback).
- `decision/risk_engine.py` — i gate in ordine (`kill_switch` per PRIMO) con
  verdetto `approve`/`review`/`reject`; `tighten()` puo' solo ridurre un cap.
  Include il gate di **qualita' dei dati**: copertura del modello sotto
  `min_model_coverage` (default 0.5, env `DECISION_MIN_MODEL_COVERAGE`) ->
  `review` con `DATA_QUALITY_LOW`. Un modello cieco (nessun rating, profilo
  NEUTRO di lega) oggi arriverebbe a un ordine automatico; nella catena decide
  un umano. ⚠️ Trovato scrivendo i test: la sola confidenza NON bastava
  (copertura 0 + calibrato + edge forte = 0.60, sopra la soglia di review).
- `decision/stake_engine.py` — Kelly frazionato scalato **UNA VOLTA** (blinda
  il bug del 13/09 del doppio scaling), tre cap (tier/lega/risk: vince il piu'
  stretto), cap severo fail-closed sotto il floor, controllo di liquidita'
  relativo allo stake (max(stake x 2, 25 USDC)).
- `decision/review_queue.py` — coda persistente su volume
  (`DATA_DIR/decision/reviews.json`, env `DECISION_REVIEW_QUEUE`), scrittura
  atomica, scadenza automatica al KICKOFF (mai approvare a partita iniziata),
  idempotenza, lettura fail-safe (file corrotto -> coda vuota, non sovrascritto).
- `decision/pipeline.py` — ordine fisso: kill switch -> risk -> [revisione] ->
  stake; nessuno stake senza `approve`; `resolve_review()` chiude le revisioni
  umane (approva+dimensiona, oppure rifiuta, oppure `REVIEW_EXPIRED`).
- `decision/adapters.py` — **Signal Engine sui dati reali**: legge il ledger
  (`matches` JOIN `predictions` LEFT JOIN `match_analysis`) con la STESSA
  selezione di `auto_bet._today_value_picks` (status value/strong_value/
  moderate, mercato 1X2, `esito_finale IS NULL`, finestra mobile 24h, ORDER BY
  ev DESC) e produce i `Signal`. `model_prob` da `match_analysis.prob_1/X/2`
  (pre-blend; se assente -> blend + warning `model_prob_assente`), `tier` dallo
  `status` del ledger, copertura da `team_ratings` (`n_home`+`n_away`,
  `model_coverage()` lineare con campione pieno a 8 partite), `confidence` da
  `compute_confidence()` (pesi ESPLICITI in testa al file: gate superato 0.35,
  copertura 0.30, calibrato 0.15, edge forte 0.10, libro profondo 0.10, CLV
  ±0.05 — euristica dichiarata, da ricalibrare sul ledger).
  **Sola lettura** (tripwire: nessun INSERT/UPDATE/DELETE nel sorgente) e
  NIENTE rifiltro delle quote: la difesa in profondita' che in `auto_bet`
  scartava in silenzio qui diventa un `ReasonCode` contabilizzabile
  (`ODDS_TOO_HIGH`, `NOT_FAVOURITE`...). `conn`, `resolve` (nomi squadra) e
  `depth_lookup` sono iniettabili: i test girano su un SQLite temporaneo con lo
  schema di produzione. CLI: `venv/bin/python -m decision queue`.
- CLI: `venv/bin/python -m decision demo [--bankroll N] [--mode live|sim|off]`
  (tre scenari approva/review/reject, offline) e `... -m decision queue [--json]`.

**Regole garantite da tripwire**: il `Signal` non contiene stakeholder ne'
bankroll; il Risk Engine puo' solo stringere; il kill switch risponde prima di
qualunque calcolo; `import decision` non carica `auto_bet`/`bot`/`tracker`.

**Parita' con la produzione**: `test_decision_pipeline.py` confronta su una
GRIGLIA di 54 casi il set dei `reject` del Risk Engine con i rifiuti di
`value_filter.is_sane` — devono coincidere esattamente, cosi' il nuovo percorso
non puo' divergere dal vecchio senza che un test lo dica.

**Drift resa VISIBILE (non corretta)**: `adaptive_staking.MAX_STAKE_PCT` (env
`STAKE_CAP_PCT`, **1%**) e' il cap che il BOT applica, mentre
`value_filter.MAX_STAKE_PCT` (**2%**, commento "era 1%") e' quello mostrato dai
tool schedina/`/value`. `RiskLimits` tiene entrambi (`cap_value`/`cap_display`)
e `test_decision_limits.py` verifica che ciascuno segua la PROPRIA fonte: la
divergenza resta visibile invece di nascondersi dietro un numero solo. Da
decidere se allineare (e' una scelta di strategia, non un bug tecnico).

**Test**: `test_decision_pipeline.py` (49), `test_decision_adapters.py` (20),
`test_decision_review.py` (18), `test_decision_limits.py` (18) — **105 verdi,
tutti OFFLINE** (nessun DB di produzione, nessuna rete, nessun provider: DB
SQLite temporaneo e sonde/engine iniettati). Il test e2e
`TestCatenaSuDatiReali` porta un ledger temporaneo da riga a `Signal` a
verdetto: approve (rating pieni) / review (modello cieco) / reject (quota
fuori fascia).

⚠️ **NON e' collegato alla produzione**: `auto_bet` continua a usare il
percorso attuale, quindi nessun ordine cambia finche' non si decide di
sostituirlo. Prossimi passi: (1) persistenza del `DecisionRecord` per il
feedback engine (tabella `decisions` in `tracker.py`, migrazione idempotente);
(2) approvazione Telegram sulla coda (bottoni sulla voce di `ReviewQueue`);
(3) collegamento in `auto_bet` con la parita' dei gate come rete di sicurezza.

#### Passo 1 FATTO — persistenza del `DecisionRecord` + feedback engine (14/09/2026)

La catena ora **lascia una traccia**: ogni `DecisionRecord` si scrive sul
ledger `decisions` e la telemetria e' leggibile.

- **`tracker.decisions`** (nuova tabella, migrazione idempotente
  `_ensure_decisions_table`): una riga per decisione con input (mercato,
  quota, prob modello/blend, copertura, calibrazione), verdetto + motivo
  (`ReasonCode`), stake/cap applicati, revisione umana e poi ordine ed esito.
  Colonne "late" (`order_id`, `order_status`, `esito_finale`, `profit`,
  `settled_at`) in UPDATE usano `COALESCE`: ripersistere lo stesso record
  **non cancella** ordine ed esito.
  ⚠️ **L'ordine di `_ensure_decisions_table` conta** (tabella -> colonne ->
  indici): creando gli indici prima della migrazione delle colonne,
  `_get_conn` falliva all'avvio su un `decisions` vecchio/parziale — cioe'
  avrebbe messo giu' il BOT al primo deploy su un DB gia' migrato. Trovato
  dai test (`test_migrazione_idempotente_su_tabella_vecchia`).
- **API tracker**: `save_decision(record)` (idempotente su `record_id`),
  `get_decisions(closed/verdict/limit)`, `update_decision_order(record_id,
  order)`, `settle_decisions()` (chiude **anche** `review`/`reject`: senza
  l'esito degli scartati non si misura il gate; `profit` = P/L **per unita'
  di stake**, il peso lo da' la colonna `stake`; rispetto di pausa settlement
  e sanity check come gli altri ledger), `decision_stats()`.
- **`decision/feedback.py`** (nuovo): il lato PERSISTENZA, **fuori** dalla
  pipeline (che resta orchestratore puro, senza DB — stesso schema di
  `TraceStore` in `research_graph`). `persist()`, `persist_many()`,
  `attach_order()`, `settle()`, `stats()`, `snapshot()`, `format_report()`.
  **Scritture fail-safe** (mai un'eccezione: la telemetria non deve fermare
  una puntata, ritorna `{saved, error}` e logga) e **letture read-only**.
  `store` iniettabile → i test girano su un ledger finto. Import di `tracker`
  PIGRO dentro le funzioni: il tripwire "`import decision` non carica la
  produzione" resta verde (`test_import_feedback_non_carica_tracker`).
- **`DecisionRecord.as_row()`** esteso con `selection_label`, `provider`,
  `approved_by`, `review_note` (era l'unico punto mancante per una riga
  completa).
- **CLI**: `venv/bin/python -m decision feedback [--json] [--settle]`
  (`--settle` e' l'unica scrittura, opt-in).
- **Metrica nuova per il feedback**: `decision_stats()["shadow"]` raggruppa
  per verdetto le decisioni NON giocate e chiuse — cosa sarebbe successo:
  e' il costo (o il risparmio) dei gate. Il `settled` delle giocate riporta
  hit rate, ROI flat, **ROI pesato per stake** e `gap_pp` = pnl realizzato -
  EV atteso (stessa convenzione di `predictions_summary`).

**Test**: `test_decision_feedback.py` (28 verdi, offline, DB temporaneo) +
105 dei quattro file `test_decision_*` e i focus `test_settlement_watchdog`,
`test_sx_native_settlement`, `test_bets`, `test_predictions`, `test_dedup_ml`,
`test_auto_bet*`, `test_web_api`, `test_secret_hygiene` — tutti verdi. Effetto
in produzione: al prossimo deploy nasce la tabella `decisions` (vuota finche'
la catena non viene collegata ad `auto_bet`: passo 3).

### Command pattern + Fail Fast + Observability + shadow mode (15/09/2026)

Quattro direttive del proprietario, una rifattorizzazione: **il motore di
decisione non tocca piu' nulla** — emette comandi leggeri, li fa eseguire a
gateway dedicati, si ferma al primo blocco di sicurezza e racconta tutto in log
JSON strutturati. E' collegata alla produzione in **shadow mode** (nessun
ordine reale cambia).

**Decisioni prese dall'agente su indicazione del proprietario** (chieste prima
di implementare): 1) la **pausa settlement NON blocca la puntata** (ferma
referto e feedback engine, come deciso il 14/09); 2) il collegamento e' in
**shadow mode**, non esecuzione reale (passo 3 completo solo dopo il confronto
misurato); 3) **nessuna fixture di sviluppo**: i dati finti restano confinati
nei test, la CLI continua a leggere il ledger vero.

**1) COMMAND PATTERN** — nuovi moduli, tutti puri (nessun import di
`tracker`/`auto_bet`/`bot` a livello di modulo):
- `decision/commands.py`: `CommandKind` (`persist_decision` | `place_order` |
  `notify_operators`), `Command` (dato serializzabile con `command_id`,
  **`dedup_key` stabile** e `order` = posizione nell'ordine dichiarato
  `COMMAND_ORDER`), payload **tipizzati** (`PlaceOrderPayload`,
  `PersistDecisionPayload`, `NotifyPayload`) validati all'EMISSIONE, e
  `CommandPlan` (record + comandi + eventuale blocco).
- `decision/engine.py`: `build_plan()`/`emit_many()` → **solo comandi**. Ordine
  non negoziabile: fail fast → risk → comandi. Tabella dei comandi per
  verdetto: bloccato → persist+notify (1/giorno); reject → solo persist; review
  → persist+notify; approve → persist (+ place_order SOLO se eseguibile e modo
  `live`). In `sim` NIENTE `place_order`: il ledger delle puntate simulate
  resta di `auto_bet`.
- `decision/gateways.py`: `BaseGateway` (template: tempi + cattura eccezioni,
  `_run()` da implementare), `LedgerGateway` (scrive su `decisions`),
  `PlaceOrderGateway` (DELEGA ad `auto_bet._live_fill`: non reimplementare
  l'esecuzione e' l'unico modo per non farla divergere dalla produzione;
  `dry_run=True` per ispezionare), `NotifyGateway` (POST Telegram diretto,
  `sender` iniettabile), `ShadowGateway` (vedi sotto). Nessun gateway solleva:
  un errore diventa `CommandResult(ok=False)`.
- `decision/dispatcher.py`: UNICO punto di esecuzione. Instrada ogni comando al
  primo gateway che lo gestisce, apre uno **span per comando**, aggrega
  `DispatchReport` (`executed/skipped/duplicated/errors/shadow`). `fail-soft`
  per default (un comando fallito non blocca gli altri: l'audit precede
  l'ordine), `raise_on_error=True` per la semantica fail-fast opposta.

**2) FAIL FAST (`decision/guards.py`)** — catena unica e dichiarata:
`SAFETY_CHAIN` = **manual (kill switch) > daily_stop > settlement_pause**, con
`require_clear(kills, stage=...)` che **solleva `SafetyBlockError`** al primo
blocco attivo (porta il `SafetyBlock` dentro: il chiamante non ricostruisce il
motivo). Gli **stadi** rendono onesta la semantica: `betting` (kill switch,
stop-loss) e `settlement` (pausa). Per lo stadio `betting` la pausa e' un
**avviso** (`advisories()`), non un blocco. Nel motore il fail-fast e' reale:
con un blocco attivo il record esce con **`stake is None`** (nessun calcolo a
valle) e i comandi sono solo persist+notify. Direzioni fail-safe invariate:
modalita' illeggibile → `off` (fail-closed), stop-loss illeggibile → non
attivo (fail-open).

**3) OBSERVABILITY (`decision/middleware.py`)** — eventi JSON con
`request_id` (un giro), `trace_id` (un piano), `span_id` + `parent_span_id`
(ogni passo), **`config_hash`** (impronta sha256 dei `RiskLimits` efficaci: le
soglie cambiano da env, senza impronta due decisioni diverse sembrano uguali).
`Observability.span()` emette `span.start`/`span.end` con `duration_ms` e
`span.error` che **ri-solleva** (il fail-fast non si perde nel logging). Sink
**configurabile** con `DECISION_LOG_SINK`: default JSONL sul volume
(`data/decision/events.jsonl`), `stdout`, `off`, oppure un path. Un sink
iniettato vince sull'env. **Rotazione automatica** oltre `DECISION_LOG_MAX_MB`
(default 5 MB, 2 generazioni) — il job gira ogni 60s e un log senza limite sul
volume non serve a nessuno. `redact()` maschera i campi sensibili. **Mai
un'eccezione**: un sink rotto logga un warning (una volta) e la catena va
avanti.

**4) SHADOW MODE (`decision/shadow.py` + `auto_bet._shadow_run`)** — a ogni
giro `auto_bet` valuta i segnali aperti anche con la catena nuova e **registra
i comandi che emetterebbe**, senza eseguire nulla (`Dispatcher([ShadowGateway])`,
`report.shadow=True`). Tre garanzie: (a) nessun effetto reale — niente ordini,
niente Telegram, e **nessuna riga sul ledger `decisions`** (il job gira ogni
60s: il ledger reale si riempirebbe di duplicati; il registro e' il JSONL
`data/decision/shadow_commands.jsonl`, deduplicato per `dedup_key`, con dedup
che sopravvive al riavvio leggendo la coda del file); (b) **zero crediti API**
(legge solo il ledger locale, `depth_usdc=None` → nessuna lettura di libro;
tripwire nei test che avvelena rete e socket); (c) **fail-safe totale** (una
eccezione torna come `{"error": ...}`, mai verso `auto_bet`). Fail fast anche
qui: con un blocco attivo esce **prima** di interrogare il ledger. Se non c'e'
nessun segnale aperto esce **senza emettere eventi** (1440 giri/giorno di
"nessun segnale" sarebbero solo rumore). Interruttore `DECISION_SHADOW`
(default **attiva**: non esegue nulla). Lettura/report: `decision.shadow_summary()`
+ `venv/bin/python -m decision shadow`.

**5) DATI MOCK NELLO SVILUPPO** — scelta "solo gateway di test": i finti
vivono DENTRO i test (gateway in memoria, `fill`/`sender`/`persist` iniettati),
nessuna fixture su disco, la CLI legge sempre il ledger reale. Nuovo
**`conftest.py`** (autouse): `DECISION_LOG_SINK=off` e `DECISION_SHADOW_LOG`
spostato nella `tmp_path` del test — senza isolamento una sessione di test
lasciava **1051 eventi** in `data/decision/` (osservato durante il lavoro).
Verifica end-to-end manuale (isolata con `QUOTAVERACE_DATA_DIR`, attenzione:
NON e' `DATA_DIR`): 1 segnale → approve, comandi `persist_decision` +
`place_order` (`would_order: true`, stake 20.0), ledger `decisions` **vuoto**.

**CLI nuova**: `venv/bin/python -m decision status [--json]` (istantanea del
fail-fast: catena, blocchi per stadio, avvisi, ripresa) e `... shadow [--json]
[--limit N]` (registro shadow).

**Bug reali trovati scrivendo i test** (tutti fixati): 1) `Observability.span`
passava `name=` a `event()`, che ha gia' un parametro `name` → `TypeError` su
OGNI span (il campo ora e' `span_name`); 2) `ShadowGateway` non dichiarava
`dry_run`, quindi `DispatchReport.shadow` era sempre `False` (bug silenzioso:
la shadow non si sarebbe distinta dall'esecuzione); 3) `GatewayError`
costruito dai soli `CommandResult` perdeva i motivi senza risultato (comando
senza gateway) → ora porta le stringhe d'errore; 4) `record_id` ha granularita'
al SECONDO, quindi la `dedup_key` dell'ordine deve dipendere dalla partita, non
dal record (un job ogni 60s avrebbe generato ordini duplicati) — test dedicato.

**Test**: `test_decision_commands.py` (23), `test_decision_guards.py` (19),
`test_decision_observability.py` (41), `test_decision_shadow.py` (22) — **105
nuovi, tutti OFFLINE**; l'intero pacchetto `decision` = **236 verdi** (92s).
Focus regressioni verdi:
`test_auto_bet*`, `test_bot`, `test_settlement_pause`, `test_secret_hygiene`,
`test_web_api`, `test_reports`.

### Contratto di mercato `decision/market.py` (15/09/2026)

**Perche'**: finora una quota entrava nel sistema come **dict anonimo** —
chiavi diverse per provider, tipi non controllati, timestamp a volte senza
fuso orario (la classe di bug che a valle costa: CLV su un istante ambiguo, il
punteggio di una partita in corso usato come finale). Ora la quota e' un TIPO:
`MarketQuote`.

**Campi obbligatori (tutti richiesti, nessun default silenzioso)**:
`schema_version` (dichiarata DAL PRODUTTORE, deve essere in
`SUPPORTED_SCHEMA_VERSIONS`), `event_id`, `market`, `selection`, `odds`
(**minimo 0.1**, finita), `timestamp` (UTC), `source` (provider), `gateway_id`
(chi l'ha ingerita). Contesto facoltativo: `event_name`, `league`, `home`,
`away`, `kickoff`, `selection_label`, `depth_usdc`. `extra="allow"`: i campi
sconosciuti del feed NON si perdono (restano in `extra_fields`, ispezionabili)
ma non allargano il contratto. Derivati: `quote_id` (identita' della
RILEVAZIONE), `identity_key` (evento+mercato+esito, stabile nel tempo),
`age_seconds()`, `to_signal_fields()` (il ponte verso il Signal: `outcome`
compare solo per 1X2, il contratto non inventa esiti che il Signal non accetta).

**Separazione struttura/strategia**: il contratto valida la FORMA (campi,
tipi, quota >= 0.1, timestamp con fuso, coerenza mercato/selezione, versione
schema); la STRATEGIA (fascia 1.30-1.80, EV, edge, cap) resta nel Risk Engine.
Se il contratto conoscesse le soglie, ogni cambio di strategia sarebbe un
cambio di schema.

**Normalizzazione deterministica** (tabelle esplicite, mai fuzzy):
`KEY_ALIASES` (nomi di chiave dei feed: `match_id`/`eventId` → `event_id`,
`price`/`odd` → `odds`, `provider` → `source`, `version` → `schema_version`;
**il nome canonico vince sempre**), `MARKET_ALIASES` (`h2h`/`Match Odds`
→ `1X2`, `totals`/`Over/Under` → `OU`) e `SELECTION_ALIASES`
(`home`/`casa`/`1` → `1`, `draw`/`tie` → `X`, `u` → `under`). Il confronto e'
case-insensitive e ignora i separatori (`match_odds` = `Match Odds`).
**Validazione incrociata `MARKET_SELECTIONS`**: `1X2`+`over` e `OU`+`2` sono
RIFIUTATI — e' la classe del 09/09 (un `over` saldato su un 1X2).

**Ingresso** (`parse_quote` strict, `validate_batch` non bloccante):
- `parse_quote(row, gateway_id=..., source=..., schema_version=..., assume_utc=...)`
  solleva `MarketQuoteError` con **tutti** i problemi (`issues` machine-readable,
  `QuoteErrorCode`) e **logga ognuno**: una riga `logger.error` + un evento
  JSON `market.quote_rejected` (con `error_code`, campo, gateway, source,
  event_id). Un timestamp senza fuso dice come rimediare
  (`assume_utc=True` se la fonte e' UTC); l'accettazione non emette eventi
  (niente flood: `log_accepted=True` per averli).
- `validate_batch(rows, ...)` = ingresso a LOTTI: accettate + respinte +
  `by_code()`, **mai un'eccezione** (nemmeno con righe ostili), riepilogo
  `market.batch_validated` + riga di log. Anti-flood: gli eventi di rifiuto si
  fermano a `max_events` (20) e il resto finisce in `suppressed_events`.
- `gateway_id`/`source`/`schema_version` passati come default di feed si
  applicano **solo se assenti** nella riga: mai una sovrascrittura silenziosa.
- Nei log NON finisce mai il payload intero: solo nome del campo e valore
  troncato (`TRUNCATE`, 80 char).

**CLI**: `venv/bin/python -m decision market [--file F | --stdin] [--gateway ID]
[--source NOME] [--assume-utc] [--json]` — senza input valida due esempi
integrati (uno conforme, uno respinto su 4 regole). Esce **1** se c'e' almeno
un rifiuto (uso in script), **2** se il JSON non e' leggibile. La CLI usa un
sink nullo: ispeziona i contratti, non scrive sul volume.

**Bug reali trovati scrivendo i test**: 1) una riga ostile (un `Mapping` il cui
`.get` solleva) faceva uscire l'eccezione dall'handler del lotto → la
contabilita' del lotto ora non si fida del dato (`_safe_keys`/`_safe_get`);
2) l'errore di lettura del file/stdin nella CLI era un traceback → ora messaggio
pulito + exit 2.

**Test**: `test_decision_market.py` — **114 verdi, tutti OFFLINE** (dati finti
costruiti a mano, `ListSink` in memoria: **zero crediti API**); pacchetto
`decision` = **350 verdi**. Focus regressioni verde: `test_secret_hygiene`,
`test_auto_bet*`, `test_bot`, `test_risk_guards`, `test_web_api`.
⚠️ Il contratto **non e' ancora usato da nessun feed**: e' il confine pronto per
l'adapter SX/the-odds-api, da collegare quando si sostituira' l'esecuzione.

### Gateway di mercato: SX Bet sorgente PRIMARIA + stop fino alla validazione (15/09/2026)

**Direttiva del proprietario**: SX Bet come sorgente primaria dei dati di
mercato, **refresh forzato del gateway PRIMA del Risk Engine**, blocco del
sistema in caso di fallimento, tracciabilita' di `request_id`, `trace_id`,
`gateway_id`, `schema_version` e `config_hash`, e **stop delle puntate
automatiche mantenuto fino alla validazione** del feed.

**`decision/feeds.py` (nuovo)** — chi porta dentro le quote:
- `SxBetSource` (PRIMARIA): riusa la discovery di `sx_signals`
  (`_discover` + `_books_parallel`: stessa pagina `/markets/active`, stesso
  raggruppamento dei 3 mercati binari, stesso order book taker) con **import
  pigro** — `import decision` resta leggero (tripwire). Provider iniettabile →
  la suite e' OFFLINE. **Zero credenziali, zero crediti, zero ordini**; le
  letture sono pubbliche.
- `MarketFeed` (il gateway): sorgenti ordinate per priorita'
  (`SOURCE_REGISTRY`, primaria da `DECISION_FEED_PRIMARY`, default `sxbet`;
  **nome sconosciuto = nessuna fonte**, mai un ripiego silenzioso), contratto
  validato all'ingresso con `validate_batch` (un'unica porta: nessuna sorgente
  puo' aggirarlo), **refresh forzato** (`refresh(force=True)`; `--force` dalla
  CLI), finestra di riuso `DECISION_FEED_REFRESH_MIN_SEC` (default 600s: il job
gira ogni 60s e l'exchange va rispettato — "forzato" = *non usare una cache
vecchia*, non *martellare*), stato su volume
  (`DATA_DIR/decision/feed_state.json`, scrittura atomica).
- `verify_feed()` (gate, puro): **fail-closed** in quest'ordine —
  `FEED_MISSING` (nessun refresh) → `FEED_UNAVAILABLE` (nessuna sorgente ha
  risposto, o quote non conformi al contratto) → `FEED_STALE` (oltre
  `DECISION_FEED_MAX_AGE_MIN`, default 20) → `FEED_NOT_VALIDATED` (serie
  incompleta) → `ok`. Il blocco e' una regola della stessa catena
  (`SafetyBlock`, stage `market`, **precedenza 4**: dopo kill switch, stop-loss e
  pausa settlement — un problema tecnico non scavalca mai un'autorita' umana).
- **VALIDAZIONE = serie, non timbro**: `DECISION_FEED_MIN_REFRESHES` (3) refresh
  consecutivi ok — "ok" = una sorgente ha risposto E zero quote respinte dal
  contratto — **e** almeno una quota validata in totale (un feed vuoto non prova
  nulla). Un fallimento azzera il contatore; lo stato sopravvive ai redeploy; un
  file di stato CORROTTO vale come **non validato** (qui l'incertezza non deve
  aprire le puntate).

**Catena** (`engine.build_plan`): il gate di mercato sta **dopo le autorita' e
prima del Risk Engine**. Con `MarketFeed` il refresh e' forzato DENTRO il motore;
`emit_many` fa **UN refresh per giro** (non uno per segnale). Il default di
`feed_required` segue l'ambiente (`DECISION_FEED_ENABLED`, ON): in produzione il
gate e' obbligatorio, un `DECISION_FEED_ENABLED=0` esplicito lo disattiva (scelta
loggata, non silenziosa). L'identita' del feed viaggia nel piano
(`CommandPlan.market`) e su OGNI refresh viene emesso `feed.refreshed`/
`feed.failed` con i 5 identificatori (`config_hash` = impronta dei limiti
`RiskLimits`, come nel middleware).

**Percorso d'ORDINE (`auto_bet._market_feed_gate`)**: e' li' che oggi si ordina
davvero, quindi il gate vale anche li' — se il feed non e' valido il giro
**non parte** (nessun ordine, nessuna riga sul ledger; vale anche per SIM, che
alimenta ML/CLV). Fail-closed anche sulle eccezioni impreviste. Notifica admin
una-volta-al-giorno (chiave `FEED_BLOCKED`, inserita dopo i controlli KS e
stop-loss nel job). Cosi' **lo stop resta finche' il feed non e' validato**: non
serve armare a mano, si riapre da solo alla validazione (o prima con la CLI).

**CLI**: `venv/bin/python -m decision feed [--json] [--refresh] [--force]` —
stato, validazione, freschezza e identificatori; `--refresh` esegue UN refresh
reale (lettura pubblica SX). Exit 1 se il gate bloccherebbe le puntate. La CLI
usa un sink nullo: ispeziona, non scrive eventi sul volume.

**Verifica REALE dell'adapter (15/09, 12:37 UTC)**: `--refresh --force` → **81
quote** conformi al contratto, 0 respinte, 27 eventi 1X2 in finestra (Coppa
Italia, La Liga, Superettan...), `inv_sum` riportato come dato (1.0188),
`market_hash` e `sport_x_event_id` nei campi extra; validato al 3° refresh
consecutivo, gate `ok`. Esempio di quota: Genoa–Südtirol `2` @ 6.8966, depth
279 USDC, kickoff UTC.

**2 BUG REALI trovati scrivendo/verificando** (entrambi fixati):
1) **Chiave del book SX**: l'order book espone il lato scommesso con la chiave
   INTERA `1` (la `2` e' il complementare "Not X"), non con la stringa
   dell'esito → il feed risultava **vuoto** col refresh vero (0 quote su 27
   eventi). Ora legge `book.get(1)` come `sx_signals.scan`; se NESSUN book e'
   leggibile la sorgente e' dichiarata **giu'** (`SourceUnavailable`) invece di
   sembrare un mercato vuoto.
2) **`secure_logging` corrompeva gli argomenti con un dict** (BUG DI
   PRODUZIONE, pre-esistente): `logger.warning("... %s", dati)` con un dict fa
   mettere il DICT in `record.args`; il filtro lo iterava come sequenza →
   `TypeError: not all arguments converted during string formatting` alla
   scrittura, cioe' un log che rompe l'handler (emerso da
   `test_bot.py + test_decision_adapters.py` nello stesso processo). Ora i
   mapping sono mascherati MANTENENDO il mapping (test dedicato in
   `test_secure_logging.py`).

**Test**: `test_decision_feed.py` (55, tutti OFFLINE con provider SX finto) +
`test_auto_bet.py::TestGateDiMercato` (6: feed non validato/refresh
fallito/nessuna sorgente → 0 puntate, `market_gate_status()`, errore imprevisto
fail-closed, feed disattivato non blocca). Pacchetto `decision` = **405 verdi**;
focus regressioni verde: `test_auto_bet*`, `test_bot`, `test_secure_logging`,
`test_risk_guards`, `test_settlement_pause`, `test_web_api`.
`verify_guardrails.py` resta offline (`DECISION_FEED_ENABLED=0`: la diagnostica
non tocca la rete) e i 5 guardrail A-E continuano a bloccare.

**Env dichiarate** in `.railway/railway.ts` (blocco `decisionEnv`, solo servizio
`api`: il cron surebet non usa il pacchetto): `DECISION_FEED_ENABLED`,
`DECISION_FEED_PRIMARY`, `DECISION_FEED_MAX_AGE_MIN`,
`DECISION_FEED_MIN_REFRESHES`, `DECISION_FEED_REFRESH_MIN_SEC`,
`DECISION_FEED_STATE`, `DECISION_SHADOW`, `DECISION_LOG_SINK`,
`DECISION_LOG_MAX_MB`, `DECISION_SHADOW_LOG`, `DECISION_OBSERVABILITY`,
`DECISION_MIN_MODEL_COVERAGE`, `DECISION_REVIEW_*`. `railway config plan` dopo
la modifica: **0 to add, 1 to change, 0 to destroy** (l'unico cambio e' il flag
non distruttivo di api-volume). ⚠️ Le env NON sono ancora su Railway: valgono i
default di codice (feed ON, primaria sxbet, 3 refresh, 20 min, 600s).

**⚠️ STATO OPERATIVO (verificato sul container il 15/09)**: `auto_bet_mode.json`
= **`{"mode": "live"}`** (armato dal 12/09) e `settlement_paused.json` =
`{"paused": false}`, `STAKE_CAP_HARD=1`, `DECISION_FEED_ENABLED` assente →
feed ON di default. Con il codice attuale **il gate di mercato terra' ferme le
puntate finche' il feed non e' validato** (3 refresh conformi consecutivi): la
validazione si accumula da sola quando il percorso ordini gira, oppure a
richiesta con `python -m decision feed --refresh --force` (lettura pubblica SX,
**zero crediti** e nessun ordine) — utile per non aspettare i giri del job.
Il kill switch resta un'autorita' superiore: `/autobet off` ferma tutto a
prescindere dal feed.

**⚠️ Stato**: in produzione la catena resta **shadow** (nessun ordine cambia).
Restano da fare, in ordine: (a) ✅ coda revisioni su Telegram con bottoni
approva/rifiuta e callback idempotenti (15/09, `decision/review_telegram.py`:
vedi l'ultima sezione); (b) leggere il registro
shadow dopo qualche giorno e confrontarlo con le puntate reali; (c) ✅ adapter
di feed reale (`decision/feeds.py`, SX primaria) collegato al gate della catena
E al percorso d'ordine; (d) **lasciar girare il feed fino alla validazione**
(3 refresh conformi consecutivi) e poi leggere il registro shadow; (e) solo
dopo, sostituire l'esecuzione di `auto_bet` col percorso Command (passo 3).
Env nuove da dichiarare in `preserve()` di `.railway/railway.ts` prima di un
`config apply`: `DECISION_SHADOW`, `DECISION_LOG_SINK`, `DECISION_LOG_MAX_MB`,
`DECISION_SHADOW_LOG`, `DECISION_OBSERVABILITY`, `DECISION_MIN_MODEL_COVERAGE`,
`DECISION_REVIEW_ENABLED`, `DECISION_REVIEW_QUEUE`, `DECISION_REVIEW_CONFIDENCE`.


### Revisioni umane su Telegram: callback idempotenti (15/09/2026)

Chiude il punto (a) dei prossimi passi della catena `decision/`: la coda delle
revisioni (`reviews.json`) non era piu' un file che nessuno guardava — ora il
verdetto `review` diventa un **messaggio con due bottoni** e il click e'
idempotente.

**Nuovo modulo `decision/review_telegram.py`** (nessun import di produzione a
livello di modulo: `import decision` resta leggero, tripwire in
`test_decision_review_telegram.py`):

| pezzo | cosa fa |
|---|---|
| `callback_id(record_id, action)` | chiave `rv:<a\|r>:<token10>` **stabile** (funzione pura): lo stesso bottone ha sempre la stessa chiave, prima e dopo un redeploy. Il `record_id` NON viaggia nel callback (e' un digest): un payload manomesso non puo' puntare a una revisione arbitraria. |
| `parse_callback(data)` | STRETTO: prefisso + azione + token validati; tutto il resto e' `CallbackError`. `is_ours()` distingue "non nostro" (silenzio) da "nostro malformato" (errore loggato). |
| `CallbackStore` | store sul volume (`DATA_DIR/decision/review_callbacks.json`, env `DECISION_CALLBACK_STORE`), scrittura atomica: `resolved` (idempotenza) + `prompts` (anti-spam). Fail-safe: file corrotto = store vuoto e **non** sovrascritto; scrittura impossibile = `False`, mai un'eccezione. |
| `build_prompt(entry)` | testo + tastiera inline (✅ Approva / ❌ Rifiuta). Pura: si ispeziona in un test senza Telegram. Mostra quota, mercato, blend, edge, EV, confidenza, **copertura ratings**, motivo, kickoff e modalita'. |
| `send_prompts(...)` | invia i prompt agli admin (`ADMIN_CHAT_ID`), marca il prompt sullo store (idempotente): il job gira spesso e non ripete. Invio fallito → marker NON scritto (si ritenta). |
| `handle_callback(data, ...)` | chiude la revisione: `pipeline.resolve_review` + `engine.plan_for_resolved` + `Dispatcher`. Ritorna SEMPRE un `ReviewOutcome` (mai un'eccezione). |
| `answer_callback(query, ...)` | risponde alla callback query E modifica il messaggio (esito + bottoni rimossi). La risposta viene inviata anche sui duplicati: e' cio' che ferma i redelivery di Telegram. |

**Idempotenza a tre livelli** (due click, un redelivery, un riavvio a meta'
lavoro = UNA decisione, UN dispatch):
1. la chiave e' stabile per costruzione;
2. lo store ritrova la chiave risolta e restituisce l'esito invariato (nessuna
   risoluzione, nessun dispatch);
3. la coda e' idempotente per conto suo (`_decide` su una voce gia' decisa).

In piu' il **claim prima, completamento dopo**: se il processo muore fra la
decisione e il dispatch, al retry si trova un claim senza esito e NON si
ri-dispatcha (meglio una revisione a meta', visibile, che due ordini). Un
callback in `error` si sblocca SOLO a mano (`store.release`): l'idempotenza non
si rompe da sola.

**Nessuna esecuzione**: il set di gateway di default e' lo `ShadowGateway`.
Il click attraversa tutta la catena (decisione, stake, comando d'ordine) ma
*registra*: l'ordine che sarebbe partito finisce nel registro shadow, non
sull'exchange. Per eseguire davvero servono gateway espliciti — oggi nessun
chiamante di produzione li passa (e c'e' un test che lo verifica).

**La coda non si allaga**: `ReviewQueue.add` ora deduplica anche per
`signal_id`, non solo per `record_id` (che porta i secondi e quindi cambia a
ogni giro del job): **una revisione per opportunita'**, e una voce gia' decisa
(approvata o rifiutata) non torna a chiedere. Senza questo, il job ogni 60s
avrebbe prodotto centinaia di copie dello stesso segnale.

**Motore**: nuova `engine.plan_for_resolved(record)` — la mappa
record → comandi scritta UNA volta (persist sempre; `place_order` solo se
`approve` + eseguibile + `mode == live`). `build_plan` ora la usa per il
percorso approve, cosi' il callback non puo' divergere dal motore.

**bot.py**: `CallbackQueryHandler(review_callback_handler, pattern=r"^rv:")`
(solo i nostri callback; solo admin), job `decision_review_job` ogni 5 min
(`run_repeating`, `max_instances=1`) che invia i prompt, comando admin
**`/revisioni`** (stato + chiavi per la CLI). `auto_bet._shadow_run` non cambia
niente di operativo: la coda viene riempita dalla catena shadow (disattivabile
con `DECISION_REVIEWS=0`), quindi l'approvazione di un verdetto shadow resta
shadow.

**CLI**: `venv/bin/python -m decision review [--json] [--all] [--limit N]`
mostra i prompt in attesa con le chiavi; `--callback rv:a:<token>
[--reviewer N --bankroll N --mode live]` SIMULA il click percorrendo la catena
completa coi gateway shadow (idempotenza e mappa dei comandi verificabili senza
Telegram); `--send` invia davvero (rete, scelta esplicita).

**Verifica**: `test_decision_review_telegram.py` (**59 verdi, tutti OFFLINE**:
client Telegram finto, store su tmp, nessuna rete/credenziale/ordine) +
regressioni. Pacchetto `decision` = **462 verdi**. Smoke isolato end-to-end:
segnale → `review` in coda → prompt con chiave `rv:a:...` → click →
`stake 1.00`, comandi `persist_decision` + `place_order` **registrati** nel
registro shadow; **secondo click → duplicato, un solo `place_order` nel
registro**.

**Bug trovati dai test (prima del deploy)**:
1. `CallbackStore.load()` restituiva `{}` senza `resolved`/`prompts` → il primo
   giro su un volume nuovo (o con file corrotto) andava in `KeyError`: ora la
   struttura e' sempre ben formata (`_empty()`);
2. **due test trappola sulla data** (non miei, pre-esistenti): `test_sx_signals`
   (bet aperta con kickoff fisso 09/09 che, superati i 5 giorni di
   `SX_STALE_DAYS`, veniva scaduta come push) e `test_settlement_sanity`
   (partita "corrente" 31/08 fuori dalla finestra cassa di 14 giorni). Ora usano
   date RELATIVE a `now`: un test che scadeva col calendario e' un falso allarme
   che arriva sempre nel momento peggiore.

**Env dichiarate** in `.railway/railway.ts` (`decisionEnv`, solo `api`):
`DECISION_REVIEWS` (interruttore della coda) e `DECISION_CALLBACK_STORE`.
`railway config plan`: **0 to add, 1 to change, 0 to destroy**.

**⚠️ Resta shadow**: in produzione nessun ordine cambia. Il percorso ordini di
`auto_bet` continua a girare e il gate di mercato (feed SX, 3 refresh conformi)
resta l'autorita' sull'esecuzione. Prossimo passo: leggere il registro shadow
dopo qualche giorno e decidere il passo 3 (sostituire l'esecuzione col percorso
Command), con i bottoni Telegram gia' pronti a governare le revisioni.

### Fix stop-loss sull'EQUITY + monitor del ritmo crediti (15/09/2026, sera)

**1) BUG STOP-LOSS: scattava sull'ESCROW, non sulle perdite (fixato).**
`auto_bet` leggeva `availableBalance` e lo usava come bankroll, poi
`check_daily_stop()` misurava la perdita su quello. Ma `availableBalance`
**esclude i fondi in escrow delle bet aperte**: piazzare una bet abbassa il
disponibile e sembra una perdita. Prova sul volume: `start_bankroll 35.9779`,
disponibile `33.9779`, exposure `2.0` → il -5.6% era **esattamente l'escrow**
(equity reale 35.98, zero denaro perso). Timing: bet piazzate alle
`18:59:15`/`18:59:16`, stop armato alle **`18:59:25`** — 9 secondi dopo, per
24h: con ~34 USDC nel wallet bastavano **2 bet aperte** per bloccare il bot.
- `_live_wallet_balance()` → **`_live_wallet_snapshot()`**: ritorna
  `{"available", "exposure", "equity"}` (None se non leggibile/dry-run).
  `equity = available + exposure` (l'esposizione arriva da
  `get_balance()["exposure"]` = escrow + pending).
- In LIVE il **bankroll del Kelly, la drawdown protection e lo stop-loss**
  usano l'EQUITY; il **DISPONIBILE** resta il vincolo di cassa del singolo
  ordine (`_spendable`): l'equity non si spende due volte.
- `check_daily_stop(bankroll, basis=...)`: il motivo/log dichiara su quale
  valore e' misurata la perdita ("equity wallet" in LIVE, "cassa" in SIM).
- Se il provider non espone `exposure` la stima e' PRUDENTE (equity = solo
  disponibile): lo stop puo' scattare prima, mai dopo (fail-closed).
- `/autobet` mostra ora `equity (liberi + in gioco)` e il cap severo si
  misura sull'equity (prima l'operatore leggeva un bankroll diverso da quello
  usato dallo staking).
- **`verify_guardrails.py` scenario C** aggiornato (36 liberi + 2 in gioco =
  38 equity): i 5 guardrail A-E continuano a bloccare.
- Tripwire: `TestBankrollEquity` in `test_auto_bet_live.py` (bet piazzata NON
  arma lo stop — regressione del bug; perdita vera sull'equity arma ancora;
  lo stake non supera i fondi liberi; snapshot da provider finto, senza
  `exposure`, dry-run/errore → None) + `test_basis_dichiarato_nel_messaggio`
  in `test_risk_guards.py`.

**2) MONITOR DEL RITMO CREDITI (`odds_api.credit_burn_rate` +
`credit_budget_status`).** Le soglie fisse (50/20/10/5) tacciono sopra 50 e
avvisano quando il budget e' gia' compromesso; la media lunga mente (il 15/09
la finestra a 340h diceva **15.5 crediti/giorno**, le ultime 24h ne dicevano
**57.8**: la media includeva la pausa del settlement e il cambio di chiave).
- `credit_burn_rate(window_hours=48)`: consumo MISURATO fra la lettura piu'
  vecchia e la piu' recente della finestra (`remaining_ts`), con fallback
  dichiarato sulla storia disponibile; None se i crediti risalgono (chiave
  cambiata) o non c'e' niente da misurare.
- `credit_budget_status()`: residuo, ritmo, `days_left`, `exhaustion_date`,
  `sustainable_per_day` e **`alert`** = il ritmo esaurisce i crediti PRIMA del
  reset. `CREDITS_RESET` (01/10) e `days_to_reset()` ora vivono QUI (prima la
  data era duplicata in `web_api`).
- **`credit_watchdog_job`** (ogni 6h): logga SEMPRE il ritmo e allerta
  admin+iscritti con anti-spam 1/giorno (chiave `CREDIT_BURN`) quando il
  budget non arriva al reset, elencando i costi da tagliare.
- **`GET /api/credits`**: `remaining` = lettura AUTOREVOLE (l'ultima, la
  stessa di `get_remaining`), `remaining_min` = diagnostica; `status` e
  `sustainable_daily` seguono il valore vero (prima il MINIMO fra le cache
  inchiodava il numero: 12/09 reale 452, mostrato 58). `estimated_daily_consumption`
  e' il ritmo MISURATO (`consumption_source` = measured|heuristic),
  `days_left_at_current_rate` la proiezione. Fix collaterale: la chiave sport
  dei nomi cache lasciava il `.json` attaccato e non trovava il titolo lega
  (`toa_scores_soccer_italy_serie_a.json` → 'Serie A').
- Test: 7 nuovi in `test_odds_api.py` (ritmo recente, finestra vera nel
  fallback, None se risalgono, nessun alert inventato, alert/non-alert con
  `now` FISSO per non scadere col calendario) + `TestCredits` in
  `test_web_api.py` (lettura autorevole, stato coerente, consumo misurato,
  titolo lega, nessuna telemetria).

**3) MISURA REALE SUL CONTAINER (15/09, sera) — dove vanno i crediti.**
273 crediti residui, **27 chiamate `scores` in 24h (56 crediti = ~2.07 a
chiamata), ZERO chiamate di rotazione quote**: il consumo e' **tutto
settlement**, ~15-21 leghe con righe aperte riscaricate a OGNI giro del
watchdog (ogni 4h, "le leghe con righe aperte si interrogano sempre"). A
57.8/giorno i 273 crediti finiscono il **~20/09**, prima del reset (01/10,
18.2/giorno sostenibili).
⚠️ **`should_query_sport()` NON copre il settlement** (e' usato solo in
`_get_odds`): sotto la soglia 50 la rotazione si riduce ma il costo dominante
no. Leva proposta (decisione del proprietario, non applicata): dare alle
leghe con righe aperte un intervallo minimo di refetch (es. 6-12h) o saltare
le heur-only sotto soglia — il risultato di una partita non cambia fra un
giro e l'altro, quindi la chiusura ritarda di ore senza costi aggiuntivi.

**4) DEPLOY del lavoro del 15/09.** La catena `decision/` (command pattern,
fail-fast, observability, shadow mode, contratto di mercato, feed SX, coda
revisioni Telegram) era **installata ma inerte**: `origin/main` era fermo a
`90a988a` (14/09) e 36 file erano non committati. Prima del push: 0 marker di
conflitto, **1688 test offline verdi** (4 lotti, `-m "not integration"`),
`compileall` OK. Dopo il deploy in produzione nascono `data/decision/` (eventi,
registro shadow, coda revisioni) e la tabella `decisions`; il **gate di
mercato** (feed SX, 3 refresh conformi) diventa l'autorita' sull'esecuzione e
le revisioni si governano dai bottoni Telegram.

**5) BUG trovato IN PRODUZIONE al primo giro dopo il deploy (fixato).** Il
log del giro 17:36 UTC diceva `adapter: 3 segnali aperti su 4 righe di ledger`
e la riga scartata era **`Atlético Madrid vs Osasuna`** con
`esito: 'Atlético Madrid'`. Causa: il ledger `predictions` **non e' omogeneo**
— `sx_signals` scrive `1`/`X`/`2`, `fixture_engine` scrive il **nome della
squadra giocata** — e `signal_from_row` pretendeva `outcome in ("1","X","2")`,
quindi scartava in silenzio proprio le righe della produzione (la catena
misurava un insieme DIVERSO da quello su cui `auto_bet` scommette: il shadow
perdeva il suo senso). Fix: nuova **`decision.adapters.canonical_outcome`**
(stessa semantica di `auto_bet._canonical_esito`) — pass-through di 1/X/2,
alias di pareggio, nome grezzo/RISOLTO (`team_names.resolve_team_name`,
iniettabile) e `team_names.same_team` per codici di stato/sigle; se il nome
coincide con ENTRAMBE le squadre o con nessuna → `None` (mai indovinare).
`signal_from_row` accetta ora `resolve=` e `iter_signals` glielo passa.
Test: `TestEsitoCanonico` in `test_decision_adapters.py` (forme canoniche,
nome squadra → 1/2, tolleranza su codici di stato, mai indovinare, riga
`Inter` → Signal con `prob_1`, regressione sul ledger misto a 4 righe).

**Primo ordine reale dopo il fix (15/09 17:36 UTC)**: bet #41 `sx-L20021478`
(Liverpool vs Tottenham, esito 1) **1.00 USDC @ 1.8561**, FULLY_FILLED
(il floor EV era 1.86 → prezzo migliore). Il candidato Atlético Madrid è
stato saltato per prezzo (`best sxbet 1.42 < floor 1.54`), esattamente il
comportamento documentato. **Shadow**: 3 segnali valutati, verdetto
`reject` x3, 3 comandi `persist_decision`, **0 ordini** dalla catena, 0
revisioni in coda (la catena nuova e' piu' severa della corsia di cassa,
com'era previsto: «shadow, nessun ordine cambia»). Wallet 33.98 liberi +
2.00 in gioco → equity 35.98. `STAKE_CAP_HARD=0` (scelta del proprietario
del 12/09): il floor 1 USDC prevale sui cap percentuali, quindi le
puntate riprendono a 1 USDC ciascuna.

### Taglio copertura settlement: il referto segue il DENARO (15/09/2026, sera)

**Direttiva del proprietario**: "riduco la copertura settlement" (l'altra
risposta, sul gate leghe della corsia auto-bet, e' stata "prima misuro").

**Misura che ha motivato il taglio** (sul container, 15/09): il settlement
costava **27 chiamate `/scores` in 24h** (~51-58 crediti/giorno a ~2 crediti
per chiamata) contro **18,1/giorno sostenibili** fino al reset del 01/10 —
esaurimento previsto ~20/09. La voce DOMINANTE erano le leghe con le **sole
previsioni aperte** (telemetria di calibrazione, nessun soldo in gioco),
riscaricate a ogni giro del watchdog (ogni 4h).

**Implementazione** (`tracker.get_leagues_with_open_rows`, `bot._update_results`):
- **`SETTLEMENT_BETS_ONLY` (default ON)**: una lega entra nel piano solo se ha
  una **PUNTATA** nel ledger (reale o simulata) aperta o chiusa da poco. Le
  previsioni delle leghe senza puntate restano aperte fino alla scadenza
  automatica (`expire_stale_sx_rows`, chiusura come push): **si perde
  telemetria di calibrazione, MAI il referto di una puntata**. Ripristino del
  comportamento esteso con `SETTLEMENT_BETS_ONLY=0`.
- **Crediti sotto `CREDIT_LOW` (50)**: la **verifica periodica** (leghe senza
  righe aperte, costo puro che non salda nulla, `SETTLEMENT_HEAL_INTERVAL_HOURS`
  36h) viene **saltata del tutto**. Una puntata aperta si referta comunque.
  Lettura crediti fallita/illeggibile -> comportamento invariato (nessuna
  verifica saltata in silenzio).
- **Politica dichiarata nei log e nel residuo**: nuova
  `tracker.settlement_coverage_policy()` ("solo-puntate,
  verifica-periodica-saltata (crediti scarsi)") stampata nella riga
  `settlement: N leghe interrogate ... politica ...`; `settlement_residue()`
  espone `bets_only` e `heal_skipped_low_credits`. Senza dichiararla, un
  residuo piu' basso sembrerebbe un referto migliore invece di una scelta.
- **Test**: `test_settlement_watchdog.TestCoperturaSettlementSoloPuntate`
  (5 test: lega senza puntate non interrogata + controprova estesa, env che
  riattiva la copertura, verifica periodica saltata sotto soglia crediti con
  la puntata che resta nel piano, crediti illeggibili che non cambiano il
  piano, politica dichiarata, tripwire sul pianificatore in `bot.py`).
  `TestResiduoSettlement` ora gira esplicitamente con `SETTLEMENT_BETS_ONLY=0`
  (la sua classificazione dei motivi e' quella a copertura estesa).

### Gate leghe sulla corsia auto-bet: MISURATO (15/09/2026, sera)

**Direttiva del proprietario**: "prima misuro" (prima di allineare la corsia
auto-bet a `STRATEGY_LEAGUES`). Misura fatta, in sola lettura sul ledger di
produzione, con il nuovo `league_gate_impact.py` (zero ordini, zero crediti,
connessione SQLite `mode=ro`).

**Procedura attuale (bug di propagazione, non scelta)**: i candidati di
`fixture_engine` e `sx_signals` NON portano la chiave `league`, quindi
`is_sane(league="")` tratta la lega vuota come AMMESSA: la strategia "solo
campionati vincenti" (5 leghe ammesse) e' applicata dalla catena `decision/`
(che infatti rifiuta con `league_not_allowed`) ma **non dalla corsia che
piazza davvero**.

**A. Il gate NON e' giudicabile sul P/L con questo ledger** (tutto lo storico,
`--source all`; le due fonti NON si sommano — il tool le separa):

| gruppo | righe | segnali giocabili (chiusi) | PUNTATE | PREVISIONI (per unita') |
|---|---|---|---|---|
| ammesse | 18 | 7 (7) | n=1, P/L +0.99, ROI +99% | 15 chiuse, ROI **+29.8%** |
| bloccate | 78 | 11 (4) | n=1, P/L -1.00, ROI -100% | 31 chiuse, ROI **-22.8%** |
| senza lega | 278 | 107 (107) | 36 chiuse, ROI -11.2% | 205 chiuse, ROI -13.2% |

**7 segnali giocabili chiusi nelle ammesse contro 4 nelle bloccate**: sotto la
soglia di affidabilita' (30) che il tool dichiara, quindi il P/L non decide
nulla. Il dato che invece conta: **278 righe su 374 (74%) non hanno lega** (le
36/41 puntate storiche senza riga in `matches`) — finche' la lega non viene
propagata il ledger non potra' decidere il gate.

**B. Il FLUSSO invece e' decisivo: il gate spegnerebbe la corsia.**
Ultimi 3 giorni (finestra mirata), 91 righe analizzate su **33 leghe**, di cui
**13 giocabili**:
- leghe ammesse: **17 righe (19%) con 6 giocabili**;
- leghe bloccate: 74 righe con **7 giocabili (il resto sono righe rejected)**.
Nel campione piu' stretto (83 righe, i soli segnali prodotti dai due cantieri
nelle ultime 72h) le ammesse davano **11 righe (13%) e 0 giocabili**, mentre le
5 giocabili erano **tutte in leghe vietate**. Le 3 bet live aperte del 15/09
sono tutte in leghe vietate (Scottish Premiership, EFL Championship, EFL Cup) e
il registro shadow ha respinto **4/4** i segnali aperti con
`league_not_allowed`.
→ Applicare oggi il gate alla corsia auto-bet = **molto vicino a zero
puntate** (0 sui segnali giocabili delle ultime 72h). La decisione resta del
proprietario: il trade-off e' "prudenza" contro "copertura dei campionati dove
la strategia e' stata validata" — e per misurare il secondo serve prima la lega
sui candidati (`fixture_engine`/`sx_signals` non la passano).

**Nuovo strumento `league_gate_impact.py`** (diagnostica, NON decisionale):
- `measure(days, source)` → bucket ammesse/bloccate/**senza lega** con righe,
  in gioco, chiuse, **segnali giocabili (e chiusi)**, dettaglio per lega
  bloccata, righe in gioco che il gate bloccherebbe subito, sezione `coverage`
  (flusso) e `reliable` + `caveat`.
- **Le due fonti non si sommano** (difetto trovato MISURANDO in produzione il
  15/09): `bets.profit` e' valuta, `predictions.profit` e' per unita' di stake
  -> con `--source all` l'aggregato si azzera (`mixed: true`) e i numeri buoni
  restano in `by_source`, uno per fonte. L'affidabilita' si conta sui **segnali
  giocabili chiusi**, non su tutte le righe: le previsioni `rejected` dicono
  cosa il gate taglierebbe ma non sono giocate.
- **Assi temporali diversi e voluti**: i bucket P/L usano la **data del match**
  (kickoff, fallback data di registrazione), la `coverage` usa la **produzione
  del segnale** (`predictions.created_at`): le previsioni nascono 1-3 giorni
  prima del kickoff.
- CLI: `venv/bin/python league_gate_impact.py [--days N] [--source bets|all] [--json]`.
- Garanzie verificate dai test: nessuna scrittura (mode=ro, nessun
  INSERT/UPDATE/DELETE nel sorgente), nessuna rete (nessun `odds_api`/
  `fetch_scores`/`sx_signals`), errori mai propagati.
- Test: `test_league_gate_impact.py` (21 verdi, ledger temporaneo).

### Shadow Validation: stato `pending` + convalida della riga persistita (16/09/2026)

**Richiesta del proprietario**: "integra la logica di Shadow Validation subito
dopo la funzione di salvataggio nel gateway di storage; ogni valutazione
persista nel database prima di essere elaborata nell'engine di convalida,
mantenendo lo stato su 'pending' e bloccando l'invio reale fino all'esito
positivo; includi record ID e trace ID nei log del middleware".

**⚠️ Nota di partenza: "Shadow Validation" NON esisteva in questo progetto.**
Prima di scrivere codice la richiesta e' stata mappata sui componenti reali e
le due scelte non ovvie sono state chieste al proprietario:

| termine della richiesta | cosa e' stato deciso |
|---|---|
| "gateway di storage" + "funzione di salvataggio" | `LedgerGateway._run` → `feedback.persist(row)` |
| "engine di convalida" | **nuovo** `decision/validation.py` (non esisteva) |
| stato "pending" | **nuovo** stato sul ledger `decisions` (la tabella non aveva `status`) |
| "bloccare l'invio reale fino all'esito positivo" | nuovo flag **opt-in** `Dispatcher(require_persist=True)` |
| "Shadow Validation" | vivere **dentro** il gateway di storage + opt-in nella shadow mode |

Decisioni prese: **(1)** stato `pending` sul ledger **+** persistenza delle
valutazioni shadow (opt-in, deduplicate); **(2)** `require_persist` come flag
**opzionale, default invariato** (fail-soft storico).

**1) IL CICLO DI VITA DIVENTA A TRE STATI.** `"persistito" non e' "approvato"`:
la riga **nasce `pending`** col salvataggio, il motore di convalida la
**rilegge dal ledger** e solo `validated` autorizza l'ordine.

- `tracker.py`: colonna `status` in `DECISION_FIELDS` (migrazione ALTER
  idempotente, come le altre), indice `idx_decisions_status`, costanti
  `DECISION_STATUS_PENDING/VALIDATED/REJECTED`, piu' `get_decision(record_id)`,
  `set_decision_status(record_id, status)`, `decision_exists_for_signal(signal_id)`,
  filtro `get_decisions(status=...)` e `decision_stats()["by_status"]`.
  Le righe legacy (status NULL) **non** vengono contate come `pending`: non si
  inventa uno stato che non c'e'.
- `decision/models.py`: `DecisionStatus` + `DecisionRecord.status` (`pending` di
  default) + i nuovi `ReasonCode` `STAKE_NOT_EXECUTABLE` e `VALIDATION_INCOMPLETE`.
  Il motore di decisione **non tocca** lo stato: lo muove solo la convalida, cosi'
  "decidere" e "convalidare" restano due atti distinti.
- Gli stati sono **duplicati di proposito** in `tracker` e `decision.models` (il
  ledger non importa il pacchetto di decisione e viceversa, tripwire incluso) e
  un test confronta le due tabelle di stringhe: non possono divergere in silenzio.

**2) `ValidatingLedgerGateway`: la convalida sta DENTRO il gateway di storage.**
Ordine esatto dei passi, garantito dal test (`persist → read → write`):

    1. SALVA      la riga sul ledger (stato `pending`)
    2. RILEGGE    cio' che e' stato scritto  ← NON l'oggetto in memoria
    3. CONVALIDA  la riga (`decision/validation.py`, motore PURO)
    4. SCRIVE     lo stato risultante (`validated` / `rejected` / `pending`)

*Perche' la convalida legge la riga e non l'oggetto*: esaminando l'oggetto, una
scrittura fallita non si vedrebbe e la catena autorizzerebbe un ordine in nome
di una decisione che sul ledger non esiste. Leggendo cio' che e' stato scritto,
"prima persistere, poi convalidare" e' una proprieta' **strutturale**.
*Perche' nello stesso gateway e non in un comando separato*: riga e stato sono
due meta' dello stesso atto di audit — tenerli insieme rende impossibile
convalidare qualcosa che non e' stato scritto, senza aggiungere un tipo di
comando che ogni fabbrica del motore dovrebbe emettere in coppia (e ricordarsi
di non dimenticare). **Nessun comando nuovo, `COMMAND_ORDER` invariato,
`by_command` della shadow mode invariato**: i 22 test preesistenti della shadow
non sono stati toccati.

**3) REGOLE DI CONVALIDA (nessuna soglia copiata, nessun fuzzy).** Il verdetto
e' gia' scritto nella riga: qui si traduce una riga in uno stato.

| riga persistita | stato | motivo |
|---|---|---|
| verdict `reject` (kill switch, feed, gate) | rejected | il motivo del rifiuto (letto, mai inventato) |
| verdict `review` | **pending** | `review_pending` (un umano puo' ancora promuoverla) |
| verdict `approve` + stake eseguibile | validated | `ok` |
| verdict `approve` senza stake eseguibile | rejected | `stake_not_executable` (cap severo/floor) |
| verdict assente/ignoto | **pending** | `validation_incomplete` |
| riga assente o senza `record_id` | **pending** | `validation_incomplete` |

`pending` blocca l'ordine esattamente come `rejected`: la differenza e' che
`pending` puo' ancora diventare `validated`, `rejected` e' definitivo.

**4) IL BLOCCO DELL'ORDINE (`Dispatcher(require_persist=True)`, opt-in).**
Un `place_order` viene **saltato** se il `persist_decision` del piano e'
fallito, se **non c'e' affatto** un `persist_decision`, o se la convalida ha
dato esito non positivo (`data["validated"] != True`). Con
`require_persist=False` (default) il dispatcher resta **fail-soft** come prima.

- **Blocca SOLO l'ordine**: audit e notifiche proseguono. Una revisione umana
  deve poter arrivare anche se il ledger ha avuto un problema — un guasto di
  telemetria non deve diventare un silenzio operativo (test dedicato).
- Un gateway di storage **senza** convalida (il vecchio `LedgerGateway`) non ha
  `data["validated"]`: vale il salvataggio riuscito (retrocompatibilita'
  esplicita, verificata dai test).
- `DispatchReport` ha ora `aborted` e `blocked_reason`, e l'evento
  `order.blocked` registra il motivo.
- I gateway di **solo audit** (`audit_only = True` su `LedgerGateway` e
  `ValidatingLedgerGateway`) sono esclusi da `DispatchReport.shadow`: senza
  questa distinzione un giro in shadow mode con la persistenza attiva si
  dichiarerebbe "non shadow" pur non avendo eseguito nulla sul mondo.

**5) TRACCIABILITA' NEI LOG DEL MIDDLEWARE.** `record_id` e `signal_id` sono
ora su `plan.dispatch`, sugli span `command.*`, su `command.result` (col campo
`validated` della convalida) e su `plan.dispatched`; il `trace_id` (e
`request_id`/`span_id`/`parent_span_id`) c'era gia' perche' arriva dal
`TraceContext`. Test: `TestTracciabilita` (5 test, con trace fissa).

**6) SHADOW MODE: persistenza OPT-IN (`DECISION_SHADOW_PERSIST`, default OFF).**
Con l'interruttore attivo `run_shadow` registra **prima** il
`ValidatingLedgerGateway` e poi lo `ShadowGateway` (il dispatcher sceglie il
primo che sa gestire il comando): il `persist_decision` scrive **davvero** sul
ledger, mentre `place_order`/`notify_operators` restano al registro shadow.
`require_persist=True` garantisce che l'ordine risulti "sarebbe partito" solo a
convalida positiva. `out` riporta `persist_enabled`, `persisted`,
`persisted_duplicates`, `order_blocked`.

**⚠️ DEVIAZIONE DICHIARATA dalla lettera della richiesta: la deduplicazione e'
per `signal_id`, NON per `record_id`.** Il `record_id` ha granularita' al
**secondo** e cambia a ogni giro del job (60s), quindi deduplicare su di esso
non deduplicherebbe nulla: la stessa opportunita' finirebbe sul ledger fino a
1440 volte al giorno. `signal_id` (match+mercato+esito) e' invece stabile.
*Limite noto e accettato*: un segnale viene registrato alla **PRIMA**
valutazione — se il prezzo si muove dopo, la riga non si aggiorna (una riga per
opportunita', non un diario di ogni giro). Il contatore `persisted_duplicates`
rende il fenomeno visibile.

**7) IMPATTO IN PRODUZIONE: ZERO, per costruzione.** `DECISION_SHADOW_PERSIST`
default OFF → la shadow mode non scrive sul ledger come prima;
`require_persist` default False → il percorso d'ordine di `auto_bet` non cambia.
Il nuovo stato `pending` **esiste** ma nessuno lo usa finche' (a) non si accende
l'interruttore o (b) non si passa al percorso Command (passo 3). I test
preesistenti della shadow che asserivano "nessuna riga sul ledger" passano
invariati e lo dimostrano.

**Test**: `test_decision_validation.py` **66 verdi, tutti OFFLINE** (gateway
finti in memoria, SQLite temporaneo, zero rete/credenziali/ordini); pacchetto
`decision` = **539 verdi**. Regressioni verdi: `test_auto_bet*` (3 file),
`test_favourites_only`, `test_risk_guards`, `test_bot`, `test_web_api`,
`test_reports`, `test_settlement_watchdog`, `test_sx_native_settlement`,
`test_secret_hygiene`, `test_performance_report`. `verify_guardrails.py`:
**A–F tutti bloccano** (invariato). `compileall` OK.

**IaC**: `DECISION_SHADOW_PERSIST` dichiarata `preserve()` nel blocco
`decisionEnv` di `.railway/railway.ts` (solo servizio `api`).
`railway config plan` dopo la modifica: **0 to add, 1 to change, 0 to destroy**
(l'unico cambio e' il flag non distruttivo `api-volume config.isCreated`).

**Prossimo passo naturale**: accendere `DECISION_SHADOW_PERSIST=1` su Railway per
far girare la convalida sui segnali veri (una riga per segnale, zero ordini) e
leggere `decision_stats()["by_status"]` + il registro shadow dopo qualche
giorno — e' il dato che serve prima di decidere il passo 3 (sostituire
l'esecuzione di `auto_bet` col percorso Command).

### Fase di confronto shadow: catena ↔ corsia (`decision/compare.py`, 16/09/2026)

Aperta su indicazione del proprietario subito dopo la Shadow Validation: le due
strade vengono messe a confronto su dati reali, in **sola lettura**, perche' il
passo 3 (sostituire l'esecuzione di `auto_bet` col percorso Command) si decida
su numeri e non su impressioni.

**Le due strade, un registro ciascuna.**

| strada | ledger | cosa contiene |
|---|---|---|
| catena (`decision/`) | `decisions` | verdetto + `ReasonCode` + stato di convalida (per ogni segnale VALUTATO) |
| corsia (`auto_bet`) | `bets` | una riga **solo se la puntata e' stata piazzata** (sim o live) |

Chiave di giunzione: **(match_id, esito canonico)**. L'esito della corsia e'
normalizzato con `decision.adapters.canonical_outcome` (import pigro): lo stesso
ledger misto che il 15/09 aveva ingannato l'adapter (nomi squadra nelle righe di
`fixture_engine`, `1`/`X`/`2` in quelle di `sx_signals`) non falsa il confronto.

**I cinque casi (esaustivi: nessuna riga sparisce in silenzio).**

| caso | catena | corsia |
|---|---|---|
| `both_play` | approva + stake eseguibile | ha puntato |
| `blocked_played` | rifiuta / non eseguibile | **ha puntato** |
| `would_play_skipped` | approva + stake eseguibile | **non ha puntato** |
| `agree_skip` | rifiuta | non ha puntato |
| `unobserved` | nessuna riga | ha puntato |

- `blocked_played` e' il numero che conta di piu': **puntate reali che la catena
  nuova avrebbe rifiutato**, col motivo (`by_reason`) e col P/L realizzato (in
  valuta, dal ledger `bets`);
- `would_play_skipped` e' il rovescio: opportunita' che la catena avrebbe
  giocato e la corsia ha saltato — P/L **per unita' di stake** (nessun denaro e'
  stato messo) e `avg_ev`; quando il monitor liquidita' ha uno scarto per quella
  partita, il motivo viene allegato come `hint` (`lane_skip_hints`, fail-safe);
- `unobserved` **NON e' una divergenza**: sono puntate senza riga nella catena
  (valutate fuori dalla finestra di persistenza shadow, aperta il 16/09 alle
  15:00 UTC). Contarle come "bloccate" sarebbe un falso: la catena non le ha mai
  viste. Finiscono fuori da `compared` e la cosa e' dichiarata nel `caveat`.

**`chain_would_play(row)`** = verdetto `approve` **e** stake eseguibile: la
stessa condizione con cui `engine.plan_for_resolved` emette `place_order`. Il
`mode` non entra di proposito — qui si misura se il GATE avrebbe fatto passare il
segnale, non quale comando sarebbe stato emesso in simulazione.

**Garanzie** (tripwire in `test_decision_compare.py`): connessione SQLite
`mode=ro` (**il test tenta un UPDATE e pretende che il DB lo rifiuti**), nessuna
istruzione di scrittura nel sorgente, `import decision.compare` che non carica
`tracker`/`auto_bet`/`bot`/`odds_api`/`sx_signals` (zero crediti, nessuna rete),
fail-safe su DB assente, **file corrotto** (probe `_assert_readable`: senza,
un file non-SQLite verrebbe letto come "nessuna tabella" e la misura sembrerebbe
vuota invece che rotta) e tabelle mancanti (volume di un deploy precedente).

**Comandi e job.**
- CLI: `venv/bin/python -m decision compare [--days N | --all] [--json]`
  (`--days` default = `DECISION_COMPARE_DAYS`, 7 giorni; `--all` = tutto lo
  storico). Exit 1 se la misura non e' disponibile.
- `bot.decision_compare_job` (ogni 6h, `first=900`, `max_instances=1`): logga
  SEMPRE il riepilogo (e' la serie storica della fase) e notifica **solo gli
  admin** — e' materiale di ingegneria, non un segnale per gli iscritti — e solo
  se c'e' almeno una divergenza, con anti-spam 1 alert/giorno (chiave
  `SHADOW_COMPARE`). Zero costi: legge i due ledger locali.
- Env dichiarate in `preserve()` (`.railway/railway.ts`, blocco `decisionEnv`):
  `DECISION_COMPARE_ENABLED` (default ON: sola lettura) e `DECISION_COMPARE_DAYS`.
  `railway config plan` dopo la modifica: **0 to add, 1 to change, 0 to destroy**.

**⚠️ Cosa NON e' ancora giudicabile.** Le righe di `decisions` esistono solo dal
16/09 15:00 UTC: all'apertura della fase il campione e' minuscolo e il verdetto
sul P/L resta sospeso sotto `MIN_RELIABLE_CLOSED` (20 puntate chiuse fra
`both_play` e `blocked_played`), come `league_gate_impact` aveva gia' insegnato
("il P/L non e' conclusivo, il flusso si'"). Il campo `caveat` dichiara sempre il
campione: nessun ROI verra' letto come verita' prima della soglia.

**Test**: `test_decision_compare.py` (**40 verdi, tutti OFFLINE**: ledger SQLite
temporaneo con lo schema di produzione, nessuna rete, nessun provider) +
regressioni verdi (`test_decision_*` = 399, `test_bot`, `test_auto_bet*`,
`test_favourites_only`, `test_risk_guards`, `test_secret_hygiene`,
`test_liquidity_monitor`) e `verify_guardrails.py` con **A–F tutti bloccanti**.
**Bug trovato dai test**: `lane_skip_hints` riceveva `days=0` ("tutto lo
storico") e lo passava al monitor, dove una finestra a 0 taglia OGNI evento (il
cutoff diventa `now`) — gli indizi sparivano in silenzio proprio nel caso "tutto
lo storico". Ora `0` viene tradotto in `None` prima della chiamata.

### Superfici tennis: parser Challenger/ITF/WTA + backfill (16/09, sera)

Direttiva del proprietario: ridurre la percentuale di superfici sconosciute del
sandbox tennis (era 74%: 122/164 osservazioni) aggiornando il parser dei
metadati tornei. Due commit (`011eaa5` parser, `b5634bb` backfill), entrambi
deployati e verificati sul container.

**1) PARSER ESTESO (`tennis_sandbox.detect_surface`).** Le etichette REALI del
ledger `data/tennis_sandbox/ledger.db` non riconosciute erano 7 tornei:
Szczecin, Biella, Tiburon, Rennes, Guangzhou, Phan Thiet, Guadalajara.
Superficie di OGNI torneo verificata su fonte esterna (Wikipedia, campo
"Surface" del box) prima di aggiungerla — mai indovinata: **Szczecin e Biella =
clay** (terra rossa outdoor; per Biella distinta la Challenger ATP 2026
dall'omonimo ITF femminile indoor defunto), **Tiburon/Rennes/Guangzhou/Phan
Thiet/Guadalajara WTA = hard** (Rennes indoor, il resto outdoor). Matching
deterministico invariato (`_SURFACE_KEYWORDS`, pesi 2 torneo/1 parola,
ambiguita' → None); la variante diacritica "Phan Thiết" e' coperta dalla
normalizzazione esistente. Test: `test_challenger_ita_wta` in
test_tennis_sandbox.py + verifica post-deploy sul container (11/11 etichette
OK, incluso il fallback None).

**2) BACKFILL (`tennis_sandbox.backfill_surfaces()` + CLI
`--backfill-surfaces`).** L'ELO impara la superficie dal campo della riga AL
MOMENTO DEL SALDO (`settle`), quindi le righe salvate col parser vecchio e
ANCORA APERE avrebbero continuato a non insegnare nulla alle superfici. Il
backfill riempie `surface` su signals/observations col parser corrente: SOLO
righe vuote, mai una superficie gia' registrata, tornei non riconosciuti
restano ''. Idempotente e fail-safe (DB assente → contatori a zero). **Esito
sul container: 56 segnali + 133 osservazioni aggiornati, 0 sconosciuti**;
secondo giro 0/0/0 (idempotenza verificata sul ledger reale).

**3) COPERTURA DOPO IL BACKFILL (misurata sul container, 16/09 19:42 UTC).**
Sconosciuta **74% → 0%**: clay 65 osservazioni (51 saldate), hard 117 (95
saldate). Totali ledger: 182 osservazioni (146 saldate), 109 segnali, 82
chiusi (29V/51P), ROI −8.83% vs avg_ev +38.47% (la sovrastima EV resta il
problema da tarare, ora misurabile per superficie).

**4) LIMITE NOTO — l'apprendimento superficie-specifico NON e' retroattivo.**
I 146 match gia' saldati lo erano con surface vuota (aggiornavano solo
l'overall): `ratings.json` ha 269 giocatori e **0 rating di superficie**. I
rating per superficie cominciano ad accumularsi dai 36 match ancora aperti
e da quelli futuri (le cui righe ora portano la superficie corretta).
Eventuale ricostruzione storica = replay ordinato di tutte le osservazioni
saldate con ELO da zero (deciso solo se il proprietario lo richiede: ogni
replay sovrascrive la storia dei rating).

### Strategia T-60 + 4 circuit breakers + gate leghe in corsia (17/09/2026)

Direttiva del proprietario: la **decisione esecutiva** di una partita si prende
in una FINESTRA di 10 minuti (T-60..T-50 dal fischio), con micro-allocazioni e
quattro **circuit breakers** attivi PRIMA di qualunque ordine reale. Codice
deployato assieme (commit in corso): `auto_bet.py`, `bot.py`,
`decision/models.py`, `decision/stake_engine.py`, `fixture_engine.py`,
`sx_signals.py`, `conftest.py`, `verify_guardrails.py`,
`.railway/railway.ts`, due file di test NUOVI
(`test_t60_breakers.py`, `test_league_gate.py`).

**1) FINESTRA ESECUTIVA T-60..T-50 (`auto_bet.t60_window`).** Apertura
`T60_WINDOW_MIN_MIN` (60), chiusura `T60_WINDOW_MAX_MIN` (50). Verdetto:
`before` (kickoff oltre i 60'), `within` (finestra), `missed` (< 50' o gia'
iniziata), `unknown` (kickoff non parsabile) — gli ultimi due sono
**fail-closed**: non si ordina. Con `T60_EXECUTION_ONLY` (default **ON**) la
corsia `run_today_bets` **fuori finestra non ordina**: il palinsesto resta
SCANSIONATO e classificato (ledger `predictions` + shadow mode completi),
nessun ordine parte. `T60_EXECUTION_ONLY=0` ripristina l'orizzonte 0.5-24h di
prima (usato dai test e dalla diagnostica). Nuovo job `bot.t60_job` ogni 60s
(`first=75`, `max_instances=1`) che chiama `auto_bet.t60_dispatch_pending()`.

**2) CB1 — HARD CAP PER ORDINE (`T60_MAX_STAKE_USDC`, default 1.00 USDC).**
NESSUN calcolo dinamico (Kelly incluso) puo' produrre uno stake sopra il tetto:
viene **SORSCRITTO** (`decision.stake_engine.size` in `mode='live'`,
`cap_source="t60_hard_cap"`), non negoziato. Il valore 1.00 = minimo ordine
eseguibile SX Bet: con la direttiva letterale 0.50 OGNI ordine sarebbe stato
scartato dal floor e il sistema sarebbe rimasto armato ma inerte
(`T60_MAX_STAKE_USDC=0.50` per tornare alla lettera). `t60_stake()` ignora il
Kelly e applica comunque i cap di portafoglio (correlazione 30%, esposizione
totale 40%) e la cassa reale. `T60_MAX_ODDS` (1.80) chiude il tetto quota.
`decision.models.t60_executable()` e' l'UNICA fonte della regola (usata dal
validatore d'ordine e dai tripwire).

**3) CB2 — KILL SWITCH PATRIMONIALE (`T60_KILL_WALLET_USDC`, default 30.0).**
Equity wallet (liberi + in gioco, MAI il disponibile: l'escrow non e' una
perdita) **≤ 30 USDC → sistema ARRESTATO**: flag persistente sul volume
(`data/execution/t60_kill.json`, scrittura atomica), 0 puntate in QUALUNQUE
modalita' finche' un admin non lo disinnesca. Lettura **fail-closed** (flag
illeggibile = blocco attivo); un wallet **non leggibile** NON arma il flag (un
errore API transitorio non deve arrestare il sistema), ma il dispatch T-60 esce
fail-closed. Alert Telegram di emergenza al primo innesco + promemoria
1/giorno (`bot.t60_kill_watch_job` ogni 6h, chiave `T60_KILL`) + comando admin
**`/t60reset`** (disinnesca, RILEGGE il wallet e si riarma da solo se l'equity
non e' risalita: protegge dal riarmo immediato dopo un top-up dimenticato).

**4) CB3 — CONTRATTO PYDANTIC RIGIDO (`decision.models.T60OrderContract`).**
`extra="forbid"`; `price > 1.0`; `league` NON vuota; **fuso orario
OBBLIGATORIO** su `kickoff`/`created_at`/`validated_at` (mai un istante
ambiguo su un ordine reale); `kickoff > created_at`; `mode='live'` ⇒ provider
presente. Un payload che viola il contratto (o CB1) e' **SCARTATO** e non
corretto in silenzio: `auto_bet.validate_order_payload` + riga sul ledger
`bets` `mode='rejected-t60'` con `stake=0.0` e il motivo — **mai** verso il
provider.

**5) CB4 — GATE DI MERCATO + LIQUIDITA'.** `t60_dispatch_pending` esce
fail-closed se il feed di mercato (SX, 3 refresh conformi) non e' validato
(nessun ordine), e l'esecuzione passa dallo STESSO `_live_fill` del giro
normale (floor EV, size al floor ≥ `max(stake × 2, 25 USDC)`, scarto
registrato in `liquidity_monitor`): la guardia non e' reimplementata, cosi'
non puo' divergere dalla produzione. Esecuzione: righe `decisions`
`approve`+`validated` in finestra, dedup `UNIQUE(match_id, esito)`; `mode=sim`
registra solo paper (`t60-sim`), `mode=live` piazza e scrive la riga `mode='live'`
con `bet_id` reale. Notifica Telegram su ogni ordine T-60 LIVE
(`bot.t60_job`, `mode='t60-live'`).

**6) GATE LEGHE APPLICATO ALLA CORSIA ORDINI (17/09, dopo la misura del
15/09).** La strategia "solo campionati vincenti" (5 leghe) non era applicata
dalla corsia che piazza DAVVERO: i candidati di `fixture_engine` e
`sx_signals` non portavano la chiave `league`, quindi `is_sane(league="")`
trattava la lega vuota come AMMESSA. Ora:
- i candidati di entrambi i motori portano `league` (la classificazione
  per-esito non e' piu' cieca);
- `auto_bet._today_value_picks` riapplica `value_filter.league_allowed`
  (difesa in profondita') ed e' **fail-closed sulla lega assente**: senza
  sapere cosa si sta giocando non si ordina;
- `sx_signals.SX_LEAGUE_ALIASES` copre le varianti con PREFISSO PAESE delle
  leghe della strategia ("England Premier League", "Germany Bundesliga",
  "France Ligue 1", "Netherlands Eredivisie", "Turkish Super Lig", ...):
  un falso DIVIETO su una lega ammessa varrebbe più di un divieto mancante
  (azzererebbe il flusso autorizzato).
⚠️ Conseguenza ATTESA (misurata il 15/09): il gate spegne quasi tutto il
flusso — nelle ultime 72h i segnali giocabili erano 0 nelle leghe ammesse e 5
in leghe vietate. E' la scelta prudente del proprietario, non un bug: per
tornare indietro serve allargare `STRATEGY_LEAGUES`, non togliere il gate.

**7) TEST E VERIFICHE.** `test_t60_breakers.py` (nuovo, 33 verdi: finestra,
CB1 sovrascrittura del Kelly, CB2 flag/persistenza/fail-safe sui wallet non
leggibili, CB3 payload malformato → riga `rejected-t60` e nessuna chiamata al
provider, CB4 dedup/parziale/sim-mai-al-provider); `test_league_gate.py`
(nuovo, 32 verdi). `verify_guardrails.py` ha ora **7 scenari (A-G)** e
l'ultimo giro e' `TUTTI I GUARDRAIL BLOCCANO` (exit 0): F = lega vietata mai
candidata + controprova su lega ammessa, G = fuori finestra T-60 solo
scansione + controprova con `T60_EXECUTION_ONLY=0`, CB1 (bankroll 10000 →
stake 1.00), CB3 (payload stake 5.00 scartato), CB2 (equity 25 → flag armato,
0 ordini). Altri due fix in questo giro:
- `conftest.py` isola `T60_KILL_FILE`/`DAILY_STOP_FILE` nella tmp dei test e
disattiva `T60_EXECUTION_ONLY`: senza isolamento un test con wallet finto
sotto i 30 USDC armava il CB2 sul percorso REALE e arrestava tutti i test
successivi dello stesso processo;
- `test_auto_bet_live.py` neutralizza la soglia CB2 (i suoi wallet finti sono
12.28/3.0 USDC, documentati in AGENTS): con la soglia vera ogni asserzione di
staking avrebbe misurato il kill switch — e i test che attendono `[]`
sarebbero passati per il motivo sbagliato. La soglia vera resta testata in
`test_t60_breakers.py`;
- `test_sx_native_settlement.test_match_recente_non_scade` usava un kickoff
FISSO (2026-09-11): col passare dei giorni e' SCADUTO da solo (6 giorni >
`SX_STALE_DAYS` 5) e falliva senza che nulla fosse rotto. Ora la data e'
RELATIVA a `now` (stessa lezione del 15/09: un test che scade col calendario
arriva sempre nel momento peggiore).

**Env** (dichiarate `preserve()` in `.railway/railway.ts`, blocco accanto a
`STAKE_CAP_HARD`, NON ancora impostate su Railway → valgono i default di
codice): `T60_EXECUTION_ONLY`, `T60_WINDOW_MIN_MIN`, `T60_WINDOW_MAX_MIN`,
`T60_MAX_STAKE_USDC`, `T60_MAX_ODDS`, `T60_KILL_WALLET_USDC`,
`T60_ORDER_VALIDATION`.

**⚠️ Punti aperti (da decidere, non bug):**
1. **Due percorsi esecutivi.** In finestra T-60 ordina la corsia
   `run_today_bets` (Kelly + cap percentuali: con wallet < 100 USDC e
   `STAKE_CAP_HARD=0` il floor 1 USDC prevale, quindi in pratica 1 USDC) e in
   parallelo `t60_dispatch_pending` ordina le righe `decisions` validate (cap
   CB1). Oggi il secondo e' INERTE in produzione (`DECISION_SHADOW_PERSIST`
   default OFF ⇒ nessuna riga `decisions`), quindi l'esecuzione reale resta la
   corsia; il dedup `UNIQUE(match_id, esito)` impedisce il doppio ordine. Da
   decidere: se il CB1 (1 USDC) debba valere anche sulla corsia Kelly (misura
   fatta il 17/09, vedi sotto) e se attivare la persistenza shadow per dare al
   dispatch T-60 le righe da eseguire.
2. **CB2 a 30 USDC con wallet ~36 USDC**: l'arresto scatta dopo ~6 USDC di
   perdite di equity. E' la soglia della direttiva — ma va ricordato che un
   arresto NON si sblocca da solo se il wallet resta sotto soglia
   (`/t60reset` rilegge e si riarma).
3. La misura del gate leghe del 15/09 e' precedente a questo deploy: dopo
   qualche giorno di ledger conviene rimisurare il flusso (quante puntate
   arrivano davvero in finestra T-60 sulle sole leghe ammesse).

#### Misura d'impatto del cap CB1 sulla corsia (17/09/2026, `t60_cap_impact.py`)

Direttiva del proprietario: prima di decidere se il cap CB1 debba valere anche
sulla corsia Kelly che piazza davvero, **misura**. Nuovo strumento di sola
LETTURA (zero ordini, zero crediti, SQLite `mode=ro`, nessuna rete) che usa
l'`adaptive_stake` REALE della produzione — non una formula ricopiata — piu' il
tripwire `test_t60_cap_impact.py` (20 verdi offline: la connessione RIFIUTA una
`UPDATE`, il sorgente non contiene scritture ne' import di rete, le soglie
seguono l'env reale).

**Perche' la risposta dipende dal BANKROLL**: con `STAKE_CAP_HARD=0`
(produzione dal 12/09) il floor dell'exchange (1.00 USDC) **prevale** sul cap
percentuale — sotto il floor lo stake viene ALZATO a 1.00 USDC, che e'
esattamente il cap CB1. Il taglio esiste solo quando il cap percentuale (1%
value/moderate, 2% strong) supera 1.00 USDC.

**Numeri (configurazione di produzione: cap severo OFF, cap 1%/2%, Kelly 5%;
con Kelly dinamico 0.05-0.40 le soglie NON cambiano: il cap percentuale domina):**

| bankroll | value/moderate | strong_value |
|---|---|---|
| 36 (equity reale 17/09) | 1.00 = | 1.00 = |
| 50 | 1.00 = | 1.00 = |
| 75 | 1.00 = | **1.50 → 1.00 (−33%)** |
| 100 | 1.00 = | **2.00 → 1.00 (−50%)** |
| 150 | **1.50 → 1.00 (−33%)** | **3.00 → 1.00 (−67%)** |
| 250 | **2.50 → 1.00 (−60%)** | **5.00 → 1.00 (−80%)** |
| 500 | **5.00 → 1.00 (−80%)** | **10.00 → 1.00 (−90%)** |

**Soglie di rottura**: `strong_value` da **50.25 USDC**, `value`/`moderate` da
**100.50 USDC** (il cap percentuale, appunto).

**VERDETTO**: con l'equity attuale (**36 USDC**) il cap CB1 non cambierebbe
NESSUNA puntata — il floor 1.00 USDC e' gia' il cap: applicarlo oggi sarebbe a
costo ZERO, e il tetto comincerebbe a proteggere quando il wallet cresce (a
100 USDC dimezza i `strong_value`, a 250-500 USDC taglia il 60-90% dello stake
Kelly). Ledger locale al momento della misura: 6 segnali vivi, **0 tagliati**.
Uso: `venv/bin/python t60_cap_impact.py [--bankroll N] [--clv] [--json]
[--db PATH]` (da rifare in sola lettura sul ledger di produzione per la
conferma sui segnali reali).
