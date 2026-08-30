import { defineRailway, github, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const api = service("api", {
    source: github("Siryochy/quotaverace", { checkSuites: false }),
    replicas: { "ams": 1 },
    env: { API_FOOTBALL_KEY: preserve(), ODDS_API_KEY: preserve(), RAILWAY_CONFIG_PATH: preserve(), RAILWAY_DOCKERFILE_PATH: preserve() },
  });
  const quotaverace = service("quotaverace", {
    replicas: { "ams": 1 },
    env: { QUOTAVERACE_BOT_TOKEN: preserve() },
  });

  return project("quotaverace", {
    resources: [api, quotaverace],
  });
});
