'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO = {
  bankroll: 500.0,
  roi_30gg: 12.4,
  segnali_oggi: 3,
  chiusi_30gg: 18,
  hit_rate: 61.1,
  ultime_value: [
    { evento: 'Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, ev: 0.155, probabilita: 0.55 },
    { evento: 'Roma vs Milan', esito: '1', quota: 2.40, ev: 0.08, probabilita: 0.45 },
  ],
  per_mercato: {},
  streaks: { current_streak: 0, current_type: 'none', max_win_streak: 0, max_loss_streak: 0 },
  clv: { n: 0, avg_raw: 0, avg_vf: 0, avg_vs_pinnacle: 0, pending: 0 },
  auto_bets: { n: 0, won: 0, lost: 0, total_stake: 0, pnl: 0, roi: 0 },
  bankroll_stats: { current: 500, peak: 500, drawdown_pct: 0, risk_level: '🟢 OK' },
}

export default function Dashboard() {
  const [data, setData] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/dashboard`)
        if (!res.ok) throw new Error('fallback demo')
        setData(await res.json())
      } catch {
        setData(DEMO)
        setUsingDemo(true)
      }
    })()
  }, [])

  const d = data || DEMO
  const streaks = d.streaks || {}
  const clv = d.clv || {}
  const ab = d.auto_bets || {}
  const pm = d.per_mercato || {}
  const bs = d.bankroll_stats || {}
  const streakTxt = streaks.current_streak
    ? (streaks.current_type === 'won'
        ? `🔥 ${streaks.current_streak} vittorie di fila`
        : `📉 ${streaks.current_streak} perse di fila`)
    : '—'

  return (
    <main className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📊 Dashboard QuotaVerace</h1>
        <Link href="/schedina" className="text-sm bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg font-medium transition">
          📋 Schedina
        </Link>
      </div>
      {usingDemo && <p className="text-amber-400 text-sm mb-8">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <Card title="💰 Bankroll" value={`€${Number(d.bankroll).toFixed(2)}`} color="emerald" />
        <Card title="📈 ROI 30gg" value={`${Number(d.roi_30gg).toFixed(1)}%`} color="blue" />
        <Card title="🎯 Segnali Oggi" value={String(d.segnali_oggi)} color="purple" />
        <Card title="✅ Hit Rate (30gg)" value={`${Number(d.hit_rate).toFixed(1)}%`} color="teal" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <Card title="🔥 Streak attuale" value={streakTxt} color="orange" small />
        <Card title="📉 Drawdown da picco" value={`${Number(bs.drawdown_pct ?? 0).toFixed(1)}% (${bs.risk_level || '?'})`} color="red" small />
        <Card title="📈 CLV vig-free" value={clv.n ? `${Number(clv.avg_vf).toFixed(2)}%` : '—'} color="blue" small />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        {/* Calibrazione per mercato */}
        <div className="bg-gray-800 rounded-xl p-6">
          <h2 className="text-xl font-bold mb-4">📚 Calibrazione per mercato</h2>
          {Object.keys(pm).length === 0 && <p className="text-gray-500 text-sm">Nessuna previsione chiusa ancora.</p>}
          <table className="w-full text-left">
            <thead><tr className="text-gray-400 border-b border-gray-600"><th className="pb-2">Mercato</th><th className="pb-2 text-right">N</th><th className="pb-2 text-right">Hit</th><th className="pb-2 text-right">ROI</th><th className="pb-2 text-right">EV att.</th></tr></thead>
            <tbody>
              {Object.entries(pm).map(([mkt, b]: [string, any]) => (
                <tr key={mkt} className="border-b border-gray-700">
                  <td className="py-2 font-medium">{mkt}</td>
                  <td className="py-2 text-right">{b.n}</td>
                  <td className="py-2 text-right">{Number(b.hit_rate).toFixed(0)}%</td>
                  <td className={`py-2 text-right font-bold ${Number(b.roi) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{Number(b.roi).toFixed(1)}%</td>
                  <td className="py-2 text-right text-gray-400">{Number(b.avg_ev).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Puntate automatiche + CLV */}
        <div className="bg-gray-800 rounded-xl p-6">
          <h2 className="text-xl font-bold mb-4">🤖 Puntate automatiche (30gg)</h2>
          {ab.n === 0 ? <p className="text-gray-500 text-sm">Nessuna puntata chiusa nel periodo.</p> : (
            <>
              <div className="grid grid-cols-3 gap-3 mb-4">
                <MiniStat label="Chiuse" value={String(ab.n)} />
                <MiniStat label="Vinte" value={String(ab.won ?? 0)} />
                <MiniStat label="P/L" value={`€${Number(ab.pnl ?? 0).toFixed(2)}`} color={Number(ab.pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'} />
              </div>
              <p className="text-gray-400 text-sm">
                Stake totale €{Number(ab.total_stake ?? 0).toFixed(2)} · ROI <b className={Number(ab.roi ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(ab.roi ?? 0).toFixed(1)}%</b>
              </p>
            </>
          )}
          <h3 className="text-lg font-bold mt-6 mb-2">🎯 CLV (30gg)</h3>
          <div className="text-sm text-gray-400 space-y-1">
            <p>Raw: <b className={Number(clv.avg_raw ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_raw ?? 0).toFixed(2)}%</b> (n {clv.n ?? 0})</p>
            <p>Vig-free: <b className={Number(clv.avg_vf ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_vf ?? 0).toFixed(2)}%</b></p>
            <p>vs Pinnacle: <b className={Number(clv.avg_vs_pinnacle ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{Number(clv.avg_vs_pinnacle ?? 0).toFixed(2)}%</b></p>
            {Number(clv.pending ?? 0) > 0 && <p className="text-amber-400">⏳ {clv.pending} segnali in attesa di chiusura</p>}
          </div>
        </div>
      </div>

      <div className="bg-gray-800 rounded-xl p-6">
        <h2 className="text-xl font-bold mb-4">⚡ Ultime Value Bet</h2>
        <table className="w-full text-left">
          <thead><tr className="text-gray-400 border-b border-gray-600"><th className="pb-2">Evento</th><th className="pb-2">Esito</th><th className="pb-2">Quota</th><th className="pb-2">EV</th><th className="pb-2">Prob</th></tr></thead>
          <tbody>
            {(d.ultime_value || []).map((v: any, i: number) => (
              <tr key={i} className="border-b border-gray-700">
                <td className="py-3">{v.evento}</td>
                <td className="py-3">{v.esito}</td>
                <td className="py-3">{Number(v.quota).toFixed(2)}</td>
                <td className={`py-3 font-bold ${Number(v.ev) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>{(Number(v.ev) * 100).toFixed(1)}%</td>
                <td className="py-3">{v.probabilita != null ? `${(Number(v.probabilita) * 100).toFixed(0)}%` : '-'}</td>
              </tr>
            ))}
            {(d.ultime_value || []).length === 0 && <tr><td className="py-3 text-gray-500" colSpan={5}>Nessuna value bet in corso.</td></tr>}
          </tbody>
        </table>
      </div>
    </main>
  )
}

function Card({ title, value, color, small }: { title: string; value: string; color: string; small?: boolean }) {
  const bc = color === 'emerald' ? 'border-emerald-500' : color === 'blue' ? 'border-blue-500' : color === 'teal' ? 'border-teal-500' : color === 'orange' ? 'border-orange-500' : color === 'red' ? 'border-red-500' : 'border-purple-500'
  return <div className={`p-6 rounded-xl bg-gray-800 border-l-4 ${bc} shadow-lg`}><div className="text-gray-400 text-sm mb-1">{title}</div><div className={`font-bold ${small ? 'text-xl' : 'text-3xl'}`}>{value}</div></div>
}

function MiniStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return <div className="bg-gray-900 p-3 rounded-lg text-center"><div className="text-gray-400 text-sm">{label}</div><div className={`text-xl font-bold ${color || ''}`}>{value}</div></div>
}