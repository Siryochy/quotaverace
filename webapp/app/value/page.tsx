'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO = [
  { evento: 'Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, ev: 0.155, probabilita: 0.55, bookmaker: 'Bet365' },
  { evento: 'Roma vs Milan', esito: '1', quota: 2.40, ev: 0.08, probabilita: 0.45, bookmaker: 'Snai' },
]

export default function Value() {
  const [signals, setSignals] = useState<any[]>([])
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/value`)
        if (!res.ok) throw new Error('fallback')
        const j = await res.json()
        if (j?.length) setSignals(j)
        else setSignals([])
      } catch { setSignals(DEMO); setUsingDemo(true) }
    })()
  }, [])

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">🎯 Value Bet</h1>
        <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        Filtri Pro: EV 3-15% · Odds 1.50-5.00 · Kelly 1/4 · Cap 3% · confronto col mercato (devig power)
      </p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}

      {signals.length === 0 ? (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-400">
          Nessun segnale che supera i filtri.<br />
          <span className="text-sm">Esegui <code className="text-blue-400">/analisi</code> nel bot per aggiornare le quote.</span>
        </div>
      ) : (
        <div className="space-y-4">
          {signals.slice(0, 10).map((s, i) => {
            const ev = Number(s.ev) * 100
            return (
              <div key={i} className="bg-gray-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="font-bold">{s.evento}</h3>
                  <span className={`text-xs px-2 py-1 rounded ${ev >= 8 ? 'bg-emerald-900 text-emerald-300' : 'bg-yellow-900 text-yellow-300'}`}>
                    {ev >= 8 ? '🔥 Valore forte' : '🟡 Valore positivo'}
                  </span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                  <div><span className="text-gray-400 block">🎯 Esito</span><b>{s.esito} @ {Number(s.quota).toFixed(2)}</b></div>
                  <div><span className="text-gray-400 block">🏦 Bookmaker</span>{s.bookmaker || '—'}</div>
                  <div><span className="text-gray-400 block">📈 EV</span><b className="text-emerald-400">+{ev.toFixed(1)}%</b></div>
                  <div><span className="text-gray-400 block">Prob</span>{s.probabilita != null ? `${(Number(s.probabilita) * 100).toFixed(0)}%` : '—'}</div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </main>
  )
}
