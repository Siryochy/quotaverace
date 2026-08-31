import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  // Volume persistente reale (creato in precedenza via CLI: api-volume).
  // Montato su /app/data (QUOTAVERACE_DATA_DIR). Railway NON supporta volumi
  // condivisi fra servizi, quindi bot + web API convivono nello stesso servizio
  // (entrypoint run_all.py): DB, data/scan_*.json, log e kill-switch sono
  // condivisi di fatto dallo stesso container.
  const data = volume("api-volume", { sizeMB: 500, region: "ams", isCreated: true });

  // Env condiviso: .env locale non viene deployato, quindi i valori vivono in
  // Railway. preserve() mantiene i valori gia' presenti e NON li esporta in
  // chiaro: una variabile non dichiarata qui sarebbe distrutta da config apply.
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
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "ams": 1 },
    volumeMounts: { ["/app/data"]: { type: "volume", name: data.name, address: data.address } },
    env: { ...sharedEnv, RAILWAY_CONFIG_PATH: preserve(), RAILWAY_DOCKERFILE_PATH: preserve() },
  });

  return project("quotaverace", {
    resources: [api, data],
  });
});