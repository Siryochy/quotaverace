import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Volume condiviso per i dati persistenti (DB, data/, log, cache, kill-switch).
  // Montato su /app/data (QUOTAVERACE_DATA_DIR) — NON su /app: Railway non usa
  // overlay, un volume sulla root nasconderebbe i sorgenti. Entrambi i servizi
  // condividono lo stesso volume, così il job 8:45 del bot scrive le scan
  // data/scan_*.json che l'API /api/scan legge.
  const data = volume("data", { sizeMB: 1024, region: "ams" });

  // Env condiviso dei servizi: .env locale non viene deployato, quindi i valori
  // vivono in Railway. preserve() mantiene i valori gia' presenti e NON li esporta
  // in chiaro: se una variabile non e' dichiarata qui, config apply la distruggerebbe.
  const sharedEnv = {
    QUOTAVERACE_BOT_TOKEN: preserve(),
    ODDS_API_KEY: preserve(),
    API_FOOTBALL_KEY: preserve(),
    BANKROLL_DEFAULT: preserve(),
    BETFAIR_APP_KEY: preserve(),
    BETFAIR_USERNAME: preserve(),
    BETFAIR_PASSWORD: preserve(),
    BETFAIR_CERT_PATH: preserve(),
    BETFAIR_CERT_KEY_PATH: preserve(),
    BETFAIR_DRY_RUN: preserve(),
    BETFAIR_LIVE: preserve(),
  };
  const api = service("api", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    replicas: { "ams": 1 },
    volumeMounts: { [data.name]: { mountPath: "/app/data" } },
    env: { ...sharedEnv, RAILWAY_CONFIG_PATH: preserve(), RAILWAY_DOCKERFILE_PATH: preserve() },
  });
  const quotaverace = service("quotaverace", {
    replicas: { "ams": 1 },
    volumeMounts: { [data.name]: { mountPath: "/app/data" } },
    env: { ...sharedEnv },
  });

  return project("quotaverace", {
    resources: [api, quotaverace, data],
  });
});