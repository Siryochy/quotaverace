'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

export default function Backtest() {
  const [data, setData] = useState<any>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/backtest`)
        if (!res.ok) throw new Error('fallback')
        setData(await res.json())
      } catch {
        setUsingDemo(true)
        setData({ n: 0, won: 0, lost: 0, hit_rate: 0, roi: 0, roi_edge: 0, gap: 0, net_units: 0, sufficiente: false, warn: false, beats_market: null, no_beats_market: null })
      }
    })()
  }, [])

  const d = data || {}
  const n = Number(d.n || 0)
  const roi = Number(d.roi || 0)
  const roiEdge = Number(d.roi_edge || 0)
  const beats = d.beats_market
  const noBeats = d.no_beats_market

  const verdict =
    n < 30 ? { icon: '⚠️', text: 'Campione troppo piccolo. Servono ≥100 scommesse chiuse per trarre conclusioni.', color: 'text-amber-400' }
    : roi >= 0 && roi >= roiEdge - 3 ? { icon: '🟢', text: 'Modello calibrato: il ROI realizzato è coerente (o migliore) dell\'EV atteso.', color: 'text-emerald-400' }
    : roi < 0 ? { icon: '🔴', text: 'Edge NON confermato: ROI negativo nonostante EV atteso positivo. Modello ottimista o varianza.', color: 'text-red-400' }
    : { icon: '🟡', text: 'Situazione ambigua: ROI positivo ma sotto l\'EV atteso. Servono più scommesse.', color: 'text-yellow-400' }

  return (
    <main className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">📊 Backtest</h1>
        <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        Confronta l'<b>EV atteso</b> dal modello con il <b>ROI realizzato</b> dagli esiti reali (flat 1 unità).
        Se ROI ≈ EV il modello è calibrato; se ROI molto &lt; EV su campione ampio, l'edge è un'illusione.
      </p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile.</p>}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <Stat label="Scommesse chiuse" value={String(n)} />
        <Stat label="Vinte / Perse" value={`${d.won ?? 0} / ${d.lost ?? 0}`} />
        <Stat label="Hit rate" value={`${Number(d.hit_rate || 0).toFixed(1)}%`} />
        <Stat label="P/L cumulato" value={`${Number(d.net_units || 0).toFixed(2)}u`} highlight={Number(d.net_units || 0) >= 0} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <Metric label="⚖ EV atteso medio" value={`${roiEdge >= 0 ? '+' : ''}${roiEdge.toFixed(2)}%`} />
        <Metric label="💰 ROI realizzato" value={`${roi >= 0 ? '+' : ''}${roi.toFixed(2)}%`} highlight={roi >= 0} />
        <Metric label="🔬 Gap calibrazione (ROI−EV)" value={`${Number(d.gap || 0) >= 0 ? '+' : ''}${Number(d.gap || 0).toFixed(2)}%`} highlight={Number(d.gap || 0) >= 0} />
      </div>

      {/* Test decisivo: edge vs mercato */}
      <div className="bg-gray-800 rounded-xl p-6 mb-6">
        <h2 className="text-xl font-bold mb-3">🎯 Edge vs mercato (closing line)</h2>
        <p className="text-gray-400 text-sm mb-4">
          La ricerca (2026) indica che il test decisivo della calibrazione è: i segnali che <b>battono il mercato</b> (devig)
          devono performare meglio di quelli che non lo battono.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-emerald-900/30 border border-emerald-500/30 p-4 rounded-lg">
            <div className="text-gray-400 text-sm">✅ Batte il mercato (edge ≥ +3pp)</div>
            <div className="text-2xl font-bold text-emerald-400">{beats ? `${beats.roi >= 0 ? '+' : ''}${beats.roi.toFixed(2)}%` : 'n.d.'}</div>
            <div className="text-sm text-gray-400">{beats ? `${beats.n} segnali` : 'nessun dato'}</div>
          </div>
          <div className="bg-gray-900 p-4 rounded-lg">
            <div className="text-gray-400 text-sm">⚠️ Non batte il mercato</div>
            <div className="text-2xl font-bold">{noBeats ? `${noBeats.roi >= 0 ? '+' : ''}${noBeats.roi.toFixed(2)}%` : 'n.d.'}</div>
            <div className="text-sm text-gray-400">{noBeats ? `${noBeats.n} segnali` : 'nessun dato'}</div>
          </div>
        </div>
      </div>

      <div className={`bg-gray-800 border-l-4 rounded-xl p-5 ${verdict.color}`}>
        <span className="text-xl">{verdict.icon}</span> <b>{verdict.text}</b>
      </div>

      <p className="text-gray-500 text-xs mt-6">
        🎲 Gioca responsabilmente. Le scommesse sono un gioco d'azzardo: non puntare più di quanto puoi permetterti di perdere.
        Se hai bisogno di aiuto, visita il portale ADM: adm.gov.it
      </p>
    </main>
  )
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return <div className={`p-4 rounded-lg text-center ${highlight ? 'bg-emerald-900/30 border border-emerald-500/30' : 'bg-gray-800'}`}>
    <div className="text-gray-400 text-sm">{label}</div>
    <div className={`text-2xl font-bold ${highlight ? 'text-emerald-400' : ''}`}>{value}</div>
  </div>
}

function Metric({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return <div className={`p-5 rounded-xl ${highlight ? 'bg-emerald-900/30 border border-emerald-500/30' : 'bg-gray-800'}`}>
    <div className="text-gray-400 text-sm mb-1">{label}</div>
    <div className={`text-3xl font-bold ${highlight ? 'text-emerald-400' : ''}`}>{value}</div>
  </div>
}
