import { NextResponse } from 'next/server'
export async function GET(req: Request) {
  const { searchParams } = new URL(req.url)
  const home = searchParams.get('home') || 'Inter'
  const away = searchParams.get('away') || 'Napoli'
  return NextResponse.json({
    home, away,
    lambda_home: 1.85,
    lambda_away: 1.12,
    prob_1: 0.52,
    prob_X: 0.24,
    prob_2: 0.24,
    over25: 0.55,
    under25: 0.45,
    btts: 0.48,
    ev_best: 'Over 2.5 (+8.5%)',
    kelly_stake: '€42.50'
  })
}
