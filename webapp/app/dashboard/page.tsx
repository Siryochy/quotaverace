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

function Card({ title, value, color }: { title: string; value: string; color: string }) {
  const bc = color === 'emerald' ? 'border-emerald-500' : color === 'blue' ? 'border-blue-500' : color === 'teal' ? 'border-teal-500' : 'border-purple-500'
  return <div className={`p-6 rounded-xl bg-gray-800 border-l-4 ${bc} shadow-lg`}><div className="text-gray-400 text-sm mb-1">{title}</div><div className="text-3xl font-bold">{value}</div></div>
}