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
data/               → cache JSON + DB sqlite + modello ensemble
backtest_mc.py      → backtest walk-forward ensemble+Kelly con Monte Carlo (ROI, MaxDD)
backup_manager.py   → backup DB+dataset ML (integrity check, rotazione, /backup)
webapp/             → Next.js (Vercel): dashboard, cassa, schedina, calendario, backtest, value...
```

**Betfair è stato RIMOSSO dall'architettura il 04/09**: moduli
`betfair_client.py`, `daily_scanner.py`, `daily_scan_job.py`,
`surebet_pipeline.py` e i relativi test non esistono più. Refertazione =
the-odds-api (`odds_api.fetch_scores`, risultati stagione corrente — la
stessa chiave delle quote); quote/CLV = the-odds-api. API-Football serve
SOLO allo storico ratings 2022-2024 (`football_hist.py`): il piano free
NON copre la stagione corrente (verificato 04/09), quindi non può saldare
le partite del 2026. auto_bet è SIM-only permanente. Tripwire
`test_betfair_removed.py`: reintrodurre Betfair (o spostare il settlement
su API-Football) rompe la suite.

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

## Stato attuale (aggiornato al 04/09/2026)

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
  settlement (`_update_results`, watchdog 2h + job serali): se una riga già
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
- **Settlement watchdog / self-healing** (`bot.py`, ogni 2h): scarica i
  risultati e salda bet/previsioni/cassa aperte anche fuori dai job serali,
  con notifica verdetti. Copre redeploy che saltano i job, cache stantie,
  API lente. VERIFICATO IN PRODUZIONE (01/09): 3 bet pendenti (Birmingham,
  Wycombe, Tranmere) saldate automaticamente al primo giro, zero interventi.
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
- **Betfair RIMOSSO dall'architettura (04/09)**: moduli, comando `/scan`,
  endpoint `/api/scan` (risponde "betfair_removed" 503) e job di scansione
  eliminati. health mostra `betfair_enabled: false` fisso (compatibilità
  frontend). Tripwire `test_betfair_removed.py`: i moduli non devono
  riapparire, i moduli attivi non devono nominare Betfair nel codice.
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
  Vincolo garantito da `test_betfair_removed.py` e dai test di settlement
  (patch del confine `odds_api.fetch_scores`).
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
- **Puntate automatiche** (`auto_bet.py`, job 08:50 ITA): **SIM-only dal
  04/09** — simula i segnali value/strong_value del giorno con la quota del
  segnale (mode='sim', nessun conto Exchange), stake **ADATTIVO** (`adaptive_staking.py`):
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
- **Test**: 547+ test verdi (la suite completa richiede ~9 min).
- **Sicurezza**: rotazioni token 01/09, 02/09 e **04/09** verificate
  (Telegram `@Calcifrrbot`, ID 8372645521). Rotazione 04/09 completata
  con Opzione A: token esposto in chat REVOCATO (getMe col vecchio → 401)
  e nuovo token attivo (getMe 200), letto dalle env Railway e portato nel
  vault cifrato locale SENZA mai apparire in chat (46 chars, diff vs
  vecchio verificata). Tripwire `test_secret_hygiene.py` rende permanente
  il vincolo "nessuna credenziale in chiaro nel codice" (vedi regola 7).

## Prossimi passi possibili (non urgenti)

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
