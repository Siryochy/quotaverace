'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

const DEMO_SIGNALS = [
  {
    match_id: 'demo1', evento: 'Inter vs Napoli', league: 'Serie A',
    esito: 'Over 2.5', quota: 2.10, ev: 0.155, status: 'strong_value',
    alert_type: 'steam', severity: 'urgent', total_move_pct: -7.5,
    first_price: 2.10, last_price: 1.94, n_snapshots: 5,
  },
  {
    match_id: 'demo2', evento: 'Roma vs Lazio', league: 'Serie A',
    esito: '1', quota: 2.40, ev: 0.08, status: 'value',
    alert_type: 'crash', severity: 'urgent', total_move_pct: -5.6,
    first_price: 2.50, last_price: 2.36, n_snapshots: 3,
  },
  {
    match_id: 'demo3', evento: 'Liverpool vs Arsenal', league: 'Premier League',
    esito: 'X', quota: 3.40, ev: 0.05, status: 'value',
    alert_type: 'rlm', severity: 'warning', total_move_pct: 3.8,
    first_price: 3.55, last_price: 3.68, n_snapshots: 6,
  },
]

const DEMO_SUMMARY = { total: 3, urgent: 2, by_type: { steam: 1, crash: 1, rlm: 1 } }

type Alert = {
  match_id: string
  evento: string
  league?: string
  esito: string
  quota: number
  ev?: number
  status?: string
  alert_type: string
  severity: string
  total_move_pct: number
  first_price?: number
  last_price?: number
  n_snapshots?: number
}

const TYPE_META: Record<string, { label: string; cls: string; desc: string }> = {
  steam: { label: '🔥 STEAM', cls: 'bg-orange-900 text-orange-300', desc: 'Sharp money in entrata: movimento improvviso del mercato.' },
  crash: { label: '🚨 CROLLO QUOTA', cls: 'bg-red-900 text-red-300', desc: 'Edge in erosione: esegui subito o scarta il segnale.' },
  rlm: { label: '⚠️ RLM', cls: 'bg-yellow-900 text-yellow-300', desc: 'Il prezzo si muove contro il pubblico: possibile money sharp.' },
}

export default function Movimenti() {
  const [signals, setSignals] = useState<Alert[]>([])
  const [summary, setSummary] = useState(DEMO_SUMMARY)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/market_signals`)
        if (!res.ok) throw new Error('fallback')
        const j = await res.json()
        setSignals(j.signals || [])
        if (j.summary) setSummary(j.summary)
        else setSignals([])
      } catch {
        setSignals(DEMO_SIGNALS)
        setSummary(DEMO_SUMMARY)
        setUsingDemo(true)
      }
    })()
  }, [])

  const s = summary

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📉 Movimenti di linea</h1>
        <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        RLM · Steam move · Crollo quota sui segnali value attivi (aggiornati dal job ogni 5&apos;).
      </p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}

      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-orange-500">
          <div className="text-gray-400 text-sm">🔥 Steam</div>
          <div className="text-2xl font-bold">{s.by_type?.steam ?? 0}</div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-red-500">
          <div className="text-gray-400 text-sm">🚨 Crollo quota</div>
          <div className="text-2xl font-bold">{s.by_type?.crash ?? 0}</div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-yellow-500">
          <div className="text-gray-400 text-sm">⚠️ RLM</div>
          <div className="text-2xl font-bold">{s.by_type?.rlm ?? 0}</div>
        </div>
      </div>

      {signals.length === 0 ? (
        <div className="bg-gray-800 rounded-xl p-8 text-center text-gray-400">
          Nessun movimento rilevante sui segnali attivi.<br />
          <span className="text-sm">I movimenti &gt; 3% compaiono qui automaticamente (job ogni 5&apos; dalle 14:00).</span>
        </div>
      ) : (
        <div className="space-y-4">
          {signals.map((a) => {
            const meta = TYPE_META[a.alert_type] || TYPE_META.rlm
            const move = Number(a.total_move_pct)
            return (
              <div key={a.match_id + a.esito} className="bg-gray-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="font-bold">{a.evento} <span className="text-gray-500 text-sm">[{a.league}]</span></h3>
                  <span className={`text-xs px-2 py-1 rounded ${meta.cls}`}>
                    {meta.label} {a.severity === 'urgent' ? '· URGENTE' : ''}
                  </span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
                  <div><span className="text-gray-400 block">🎯 Esito</span><b>{a.esito} @ {Number(a.quota).toFixed(2)}</b></div>
                  <div><span className="text-gray-400 block">📈 EV</span>{a.ev != null ? `+${(Number(a.ev) * 100).toFixed(1)}%` : '—'}</div>
                  <div><span className="text-gray-400 block">📊 Movimento</span>
                    <b className={move < 0 ? 'text-red-400' : 'text-emerald-400'}>
                      {move > 0 ? '↗' : '↙'} {move > 0 ? '+' : ''}{move.toFixed(1)}%
                    </b>
                  </div>
                  <div><span className="text-gray-400 block">Prezzo</span>{Number(a.first_price).toFixed(2)} → {Number(a.last_price).toFixed(2)}</div>
                  <div><span className="text-gray-400 block">Snapshots</span>{a.n_snapshots ?? '—'}</div>
                </div>
                <p className="text-gray-400 text-xs mt-3">{meta.desc}</p>
              </div>
            )
          })}
        </div>
      )}
    </main>
  )
}
