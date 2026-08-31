// Motore Poisson/Dixon-Coles portato da poisson_engine.py (Python) per la webapp.
// Include: probabilità punteggio, 1X2, Over/Under, BTTS, expected goals, EV e Kelly.

const MAX_GOALS = 10
const RHO = -0.15 // correzione Dixon-Coles

// Medie gol di lega
export const AVG_HOME_GOALS = 1.52
export const AVG_AWAY_GOALS = 1.28

// Coefficienti squadra curati (attacco/difesa casa+trasferta).
// ratio = forza rispetto alla media; >1 attacca/sottopone, <1 difende/subisce poco.
type TeamRatings = Record<string, { attack_home: number; attack_away: number; defense_home: number; defense_away: number }>

// Sottoinsieme delle squadre top per i campionati principali (stesse medie di leagues_data.py)
export const TEAM_RATINGS: TeamRatings = {
  // Serie A
  inter:       { attack_home: 1.45, attack_away: 1.35, defense_home: 0.65, defense_away: 0.70 },
  'ac milan':  { attack_home: 1.30, attack_away: 1.15, defense_home: 0.80, defense_away: 0.85 },
  napoli:      { attack_home: 1.35, attack_away: 1.20, defense_home: 0.75, defense_away: 0.80 },
  juventus:    { attack_home: 1.30, attack_away: 1.15, defense_home: 0.75, defense_away: 0.80 },
  atalanta:    { attack_home: 1.40, attack_away: 1.20, defense_home: 0.80, defense_away: 0.85 },
  roma:        { attack_home: 1.30, attack_away: 1.10, defense_home: 0.80, defense_away: 0.85 },
  lazio:       { attack_home: 1.20, attack_away: 1.05, defense_home: 0.85, defense_away: 0.90 },
  fiorentina:  { attack_home: 1.20, attack_away: 1.05, defense_home: 0.85, defense_away: 0.90 },
  bologna:     { attack_home: 1.15, attack_away: 1.00, defense_home: 0.90, defense_away: 0.90 },
  torino:      { attack_home: 1.05, attack_away: 0.95, defense_home: 0.90, defense_away: 0.95 },
  milan:       { attack_home: 1.30, attack_away: 1.15, defense_home: 0.80, defense_away: 0.85 },
  // Premier League
  'manchester city': { attack_home: 1.50, attack_away: 1.40, defense_home: 0.60, defense_away: 0.65 },
  arsenal:     { attack_home: 1.45, attack_away: 1.30, defense_home: 0.65, defense_away: 0.70 },
  liverpool:   { attack_home: 1.45, attack_away: 1.35, defense_home: 0.65, defense_away: 0.70 },
  chelsea:     { attack_home: 1.30, attack_away: 1.15, defense_home: 0.75, defense_away: 0.80 },
  'manchester united': { attack_home: 1.20, attack_away: 1.10, defense_home: 0.85, defense_away: 0.90 },
  tottenham:   { attack_home: 1.30, attack_away: 1.15, defense_home: 0.80, defense_away: 0.85 },
  newcastle:   { attack_home: 1.25, attack_away: 1.10, defense_home: 0.80, defense_away: 0.85 },
  'aston villa': { attack_home: 1.20, attack_away: 1.05, defense_home: 0.85, defense_away: 0.90 },
  // La Liga
  'real madrid': { attack_home: 1.50, attack_away: 1.40, defense_home: 0.60, defense_away: 0.65 },
  barcelona:   { attack_home: 1.45, attack_away: 1.35, defense_home: 0.65, defense_away: 0.70 },
  'atletico madrid': { attack_home: 1.35, attack_away: 1.20, defense_home: 0.70, defense_away: 0.75 },
  sevilla:     { attack_home: 1.10, attack_away: 1.00, defense_home: 0.90, defense_away: 0.95 },
  'real sociedad': { attack_home: 1.20, attack_away: 1.05, defense_home: 0.80, defense_away: 0.85 },
  villarreal:  { attack_home: 1.25, attack_away: 1.10, defense_home: 0.80, defense_away: 0.85 },
  // Bundesliga
  'bayern munich': { attack_home: 1.55, attack_away: 1.45, defense_home: 0.55, defense_away: 0.60 },
  'bayer leverkusen': { attack_home: 1.40, attack_away: 1.25, defense_home: 0.65, defense_away: 0.70 },
  'borussia dortmund': { attack_home: 1.40, attack_away: 1.25, defense_home: 0.70, defense_away: 0.75 },
  'rb leipzig': { attack_home: 1.35, attack_away: 1.20, defense_home: 0.70, defense_away: 0.75 },
  stuttgart:   { attack_home: 1.25, attack_away: 1.10, defense_home: 0.75, defense_away: 0.80 },
  'eintracht frankfurt': { attack_home: 1.25, attack_away: 1.10, defense_home: 0.75, defense_away: 0.80 },
  // Ligue 1
  'paris saint-germain': { attack_home: 1.55, attack_away: 1.45, defense_home: 0.55, defense_away: 0.60 },
  psg:         { attack_home: 1.55, attack_away: 1.45, defense_home: 0.55, defense_away: 0.60 },
  monaco:      { attack_home: 1.30, attack_away: 1.15, defense_home: 0.70, defense_away: 0.75 },
  marseille:   { attack_home: 1.25, attack_away: 1.10, defense_home: 0.75, defense_away: 0.80 },
  lille:       { attack_home: 1.20, attack_away: 1.05, defense_home: 0.80, defense_away: 0.85 },
  lyon:        { attack_home: 1.20, attack_away: 1.05, defense_home: 0.80, defense_away: 0.85 },
}

// migliaia di alias comuni -> chiave del dizionario
const ALIASES: Record<string, string> = {
  inter: 'inter', 'ac milan': 'ac milan', milan: 'ac milan', napoli: 'napoli',
  juventus: 'juventus', 'juve': 'juventus', atalanta: 'atalanta', roma: 'roma',
  'as roma': 'roma', lazio: 'lazio', fiorentina: 'fiorentina', bologna: 'bologna', torino: 'torino',
  'man city': 'manchester city', 'manchester city': 'manchester city',
  arsenal: 'arsenal', liverpool: 'liverpool', chelsea: 'chelsea',
  'man united': 'manchester united', 'manchester united': 'manchester united',
  'man utd': 'manchester united', tottenham: 'tottenham', 'tottenham hotspur': 'tottenham',
  newcastle: 'newcastle', 'newcastle united': 'newcastle', 'aston villa': 'aston villa',
  'real madrid': 'real madrid', barcelona: 'barcelona', 'atletico madrid': 'atletico madrid',
  'atletico': 'atletico madrid', sevilla: 'sevilla', 'real sociedad': 'real sociedad',
  villarreal: 'villarreal', 'bayern munich': 'bayern munich', 'bayern': 'bayern munich',
  'bayer leverkusen': 'bayer leverkusen', 'borussia dortmund': 'borussia dortmund',
  'rb leipzig': 'rb leipzig', stuttgart: 'stuttgart', 'eintracht frankfurt': 'eintracht frankfurt',
  'psg': 'psg', 'paris saint-germain': 'psg', monaco: 'monaco', marseille: 'marseille',
  lille: 'lille', lyon: 'lyon',
}

function normalize(name: string): string {
  return name.trim().toLowerCase().replace(/\s+/g, ' ')
}

export function resolveTeam(name: string): string | null {
  const n = normalize(name)
  return ALIASES[n] ?? null
}

function poissonPmf(k: number, lam: number): number {
  return (Math.pow(lam, k) * Math.exp(-lam)) / factorial(k)
}

function factorial(n: number): number {
  let r = 1
  for (let i = 2; i <= n; i++) r *= i
  return r
}

// Matrice punteggi con correzione Dixon-Coles, normalizzata a somma 1
export function probsMatrix(lamH: number, lamA: number): Record<string, number> {
  const cells: Record<string, number> = {}
  let total = 0
  for (let hg = 0; hg <= MAX_GOALS; hg++) {
    for (let ag = 0; ag <= MAX_GOALS; ag++) {
      let p = poissonPmf(hg, lamH) * poissonPmf(ag, lamA)
      if (hg === 0 && ag === 0) p *= 1 - lamH * lamA * RHO
      else if (hg === 0 && ag === 1) p *= 1 + lamH * RHO
      else if (hg === 1 && ag === 0) p *= 1 + lamA * RHO
      else if (hg === 1 && ag === 1) p *= 1 - RHO
      cells[`${hg}-${ag}`] = p
      total += p
    }
  }
  for (const k in cells) cells[k] /= total
  return cells
}

export function prob1x2(lamH: number, lamA: number): [number, number, number] {
  const m = probsMatrix(lamH, lamA)
  let p1 = 0, px = 0, p2 = 0
  for (const key in m) {
    const [hs, as] = key.split('-').map(Number)
    if (hs > as) p1 += m[key]
    else if (hs === as) px += m[key]
    else p2 += m[key]
  }
  return [p1, px, p2]
}

export function probOverUnder(lamH: number, lamA: number, threshold = 2.5): [number, number] {
  const m = probsMatrix(lamH, lamA)
  let pOver = 0
  for (const key in m) {
    const [hs, as] = key.split('-').map(Number)
    if (hs + as > threshold) pOver += m[key]
  }
  return [pOver, 1 - pOver]
}

export function probBtts(lamH: number, lamA: number): number {
  const m = probsMatrix(lamH, lamA)
  let p = 0
  for (const key in m) {
    const [hs, as] = key.split('-').map(Number)
    if (hs >= 1 && as >= 1) p += m[key]
  }
  return p
}

// Expected goals: ritorna [null,null] se una squadra non e' nel dataset
export function expectedGoals(home: string, away: string): [number | null, number | null] {
  const hk = resolveTeam(home)
  const ak = resolveTeam(away)
  if (!hk || !ak) return [null, null]
  const h = TEAM_RATINGS[hk]
  const a = TEAM_RATINGS[ak]
  const lamH = AVG_HOME_GOALS * h.attack_home * a.defense_away
  const lamA = AVG_AWAY_GOALS * a.attack_away * h.defense_home
  return [lamH, lamA]
}

export function computeEv(prob: number, odds: number): number {
  return prob * odds - 1
}

// Kelly frazionario (default 1/4) con cap
export function kellyFraction(prob: number, odds: number, fraction = 0.25): number {
  if (odds <= 1) return 0
  const kellyFull = (prob * odds - (1 - prob)) / odds
  return Math.max(0, kellyFull * fraction)
}

// Miglior segnale con EV, quota di mercato di riferimento e stake Kelly (1/4, cap 3%)
export function bestSignal(lamH: number, lamA: number, referenceOdds: Record<string, number> = {}) {
  const [p1, px, p2] = prob1x2(lamH, lamA)
  const [pOver, pUnder] = probOverUnder(lamH, lamA)
  const btts = probBtts(lamH, lamA)
  const candidates = [
    { label: '1 (Casa)', prob: p1, odds: referenceOdds['1'] ?? 2.0 },
    { label: 'X (Pareggio)', prob: px, odds: referenceOdds['X'] ?? 3.2 },
    { label: '2 (Trasferta)', prob: p2, odds: referenceOdds['2'] ?? 2.0 },
    { label: 'Over 2.5', prob: pOver, odds: referenceOdds['Over'] ?? 2.10 },
    { label: 'Under 2.5', prob: pUnder, odds: referenceOdds['Under'] ?? 1.85 },
    { label: 'BTTS', prob: btts, odds: referenceOdds['BTTS'] ?? 1.90 },
  ]
  let bestIdx = 0
  let bestEv = -Infinity
  candidates.forEach((c, i) => {
    const ev = computeEv(c.prob, c.odds)
    if (ev > bestEv) { bestEv = ev; bestIdx = i }
  })
  const best = { ...candidates[bestIdx], ev: bestEv }
  const kelly = kellyFraction(best.prob, best.odds)
  const stakeCap = 0.03
  const cap = Math.min(kelly, stakeCap)
  return {
    prob1: p1, probX: px, prob2: p2, over25: pOver, under25: pUnder, btts,
    best: { label: best.label, prob: best.prob, odds: best.odds, ev: bestEv, kellyPct: kelly, stakeCapPct: cap },
  }
}