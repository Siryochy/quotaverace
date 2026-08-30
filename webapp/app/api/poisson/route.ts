import { NextResponse } from 'next/server'
import { expectedGoals, bestSignal, AVG_HOME_GOALS, AVG_AWAY_GOALS } from '../../../lib/poisson'

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url)
  const home = searchParams.get('home') || 'Inter'
  const away = searchParams.get('away') || 'Napoli'

  const [lamH, lamA] = expectedGoals(home, away)
  if (lamH === null || lamA === null) {
    return NextResponse.json(
      { error: `Squadra non nel dataset (home='${home}', away='${away}').`, home, away },
      { status: 400 },
    )
  }

  const s = bestSignal(lamH, lamA)
  return NextResponse.json({
    home, away,
    lambda_home: Number(lamH.toFixed(2)),
    lambda_away: Number(lamA.toFixed(2)),
    prob_1: Number(s.prob1.toFixed(4)),
    prob_X: Number(s.probX.toFixed(4)),
    prob_2: Number(s.prob2.toFixed(4)),
    over25: Number(s.over25.toFixed(4)),
    under25: Number(s.under25.toFixed(4)),
    btts: Number(s.btts.toFixed(4)),
    avg_home_goals: AVG_HOME_GOALS,
    avg_away_goals: AVG_AWAY_GOALS,
    ev_best: `${s.best.label} (+${(s.best.ev * 100).toFixed(1)}%)`,
    kelly_stake: `€${(100 * s.best.kellyPct).toFixed(2)} (Kelly ${(s.best.kellyPct * 100).toFixed(1)}%)`,
  })
}