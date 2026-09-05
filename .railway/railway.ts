import { defineRailway, fn, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Volume persistente reale (creato in precedenza via CLI: api-volume).
  // Montato su /app/data (QUOTAVERACE_DATA_DIR). Railway NON supporta volumi
  // condivisi fra servizi, quindi bot + web API convivono nello stesso servizio
  // (entrypoint run_all.py): DB, data/scan_*.json, log e kill-switch sono
  // condivisi di fatto dallo stesso container.
  const data = volume("api-volume", { sizeMB: 500, region: "ams", isCreated: true });

  // Volume DEDICATO allo scanner surebet (cron): cache the-odds-api +
  // log dedup persistono tra un'esecuzione e l'altra (Railway NON supporta
  // volumi condivisi fra servizi → il cron ha il SUO volume).
  const surebetData = volume("surebet-volume", { sizeMB: 100, region: "ams" });

  // Env condiviso: .env locale non viene deployato, quindi i valori vivono in
  // Railway. preserve() mantiene i valori gia' presenti e NON li esporta in
  // chiaro: una variabile non dichiarata qui sarebbe distrutta da config apply.
  const sharedEnv = {
    QUOTAVERACE_BOT_TOKEN: preserve(),
    ODDS_API_KEY: preserve(),
    API_FOOTBALL_KEY: preserve(),
    BANKROLL_DEFAULT: preserve(),
    ADMIN_CHAT_ID: preserve(),
    TEST_NOTIFY_KEY: preserve(),
    // BETFAIR_* rimosse il 04/09: Betfair è fuori dall'architettura
    // (refertazione = API-Football, quote/CLV = the-odds-api, auto_bet SIM).
  };

  const api = service("api", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "ams": 1 },
    volumeMounts: { ["/app/data"]: { type: "volume", name: data.name, address: data.address } },
    env: { ...sharedEnv, RAILWAY_DOCKERFILE_PATH: preserve() },
  });

  // Servizio CRON dedicato allo scanner surebet (surebet_engine.py).
  // Railway esegue lo start command (CMD del Dockerfile.surebet = scan singolo
  // che TERMINA) a ogni scatto del cron — il processo deve uscire a fine task.
  // ⚠️ Crediti the-odds-api: la chiave e' CONDIVISA col value bot (piano free
  // ~500/mese, il calendario value ne usa ~407-460). La FREQUENZA del cron NON
  // determina le chiamate API (le limita il TTL cache SUREBET_ODDS_TTL=6h:
  // max ~8 chiamate/giorno per NBA+MLB) e SUREBET_MIN_REMAINING=50 ferma lo
  // scanner sotto i 50 crediti residui, proteggendo il budget del value bot.
  const surebet = fn("surebet", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile.surebet" },
    deploy: { cronSchedule: "*/15 * * * *" },
    volumeMounts: { ["/app/data"]: { type: "volume", name: surebetData.name, address: surebetData.address } },
    env: {
      ...sharedEnv,
      SUREBET_BUDGET: "100",
      // SETTEMBRE 2026 sotto-budget: solo MLB (in stagione, partite ogni
      // giorno = campo-test continuo del modulo). NBA e' off-season a
      // settembre: riattivare "basketball_nba,baseball_mlb" il 1° ottobre
      // (inizio stagione NBA + reset dei 500 crediti mensili).
      SUREBET_SPORTS: "baseball_mlb",
      SUREBET_ODDS_TTL: "21600",
      SUREBET_MIN_REMAINING: "50",
      SUREBET_MIN_MARGIN: "0.005",
    },
  });

  return project("quotaverace", {
    resources: [api, data, surebet, surebetData],
  });
});