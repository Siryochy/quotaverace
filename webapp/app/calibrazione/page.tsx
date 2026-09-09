'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

// Fallback dimostrativo quando il backend non è raggiungibile (stessa
// convenzione delle altre pagine: la webapp su Vercel deve comunque buildare).
const DEMO = {
  model: {
    trained: true, model_type: 'xgboost', ensemble_weight: 0.515,
    train_metrics: { accuracy: 0.9825, brier_score: 0.0354, n_samples: 57, model: 'xgboost' },
  },
  calibration: {
    status: 'fitted', fitted: true, n_cal: 17,
    pre_brier: 0.2322, post_brier: 0.1479, pre_ece: 0.2231, post_ece: 0.0,
    brier_improvement: 0.0843,
    curve: [
      { score: 0.30, calibrated: 0.33 }, { score: 0.40, calibrated: 0.40 },
      { score: 0.50, calibrated: 0.46 }, { score: 0.62, calibrated: 0.55 },
      { score: 0.75, calibrated: 0.68 }, { score: 0.85, calibrated: 0.82 },
      { score: 0.95, calibrated: 0.94 },
    ],
  },
  reliability: [
    { bin: '0.0-0.1', n: 3, confidence: 0.08, accuracy: 0.00 },
    { bin: '0.1-0.2', n: 5, confidence: 0.15, accuracy: 0.20 },
    { bin: '0.2-0.3', n: 6, confidence: 0.24, accuracy: 0.17 },
    { bin: '0.3-0.4', n: 9, confidence: 0.35, accuracy: 0.33 },
    { bin: '0.4-0.5', n: 8, confidence: 0.46, accuracy: 0.38 },
    { bin: '0.5-0.6', n: 7, confidence: 0.55, accuracy: 0.57 },
    { bin: '0.6-0.7', n: 5, confidence: 0.66, accuracy: 0.60 },
    { bin: '0.7-0.8', n: 3, confidence: 0.74, accuracy: 0.67 },
    { bin: '0.8-0.9', n: 2, confidence: 0.85, accuracy: 1.00 },
    { bin: '0.9-1.0', n: 1, confidence: 0.92, accuracy: 1.00 },
  ],
  drift: {
    status: 'drift', n: 49, window: 30,
    brier_rolling: 0.2432, brier_baseline: 0.2073,
    logloss_rolling: 0.6789, logloss_baseline: 0.6049,
    recommendation: '🔄 RETRAINING consigliato: il modello sta perdendo calibrazione sulle ultime previsioni.',
  },
  drift_history: [
    { ts: '2026-08-28T20:00:00Z', n: 15, brier_rolling: 0.1712, logloss_rolling: 0.5211 },
    { ts: '2026-08-30T20:00:00Z', n: 18, brier_rolling: 0.1804, logloss_rolling: 0.5402 },
    { ts: '2026-09-01T20:00:00Z', n: 22, brier_rolling: 0.1760, logloss_rolling: 0.5319 },
    { ts: '2026-09-03T20:00:00Z', n: 27, brier_rolling: 0.1947, logloss_rolling: 0.5810 },
    { ts: '2026-09-05T20:00:00Z', n: 33, brier_rolling: 0.2188, logloss_rolling: 0.6325 },
    { ts: '2026-09-07T20:00:00Z', n: 41, brier_rolling: 0.2356, logloss_rolling: 0.6641 },
    { ts: '2026-09-09T12:00:00Z', n: 49, brier_rolling: 0.2432, logloss_rolling: 0.6789 },
  ],
  model_file: { exists: true, updated_at: '2026-09-09T05:45:00Z' },
}

type CurvePoint = { score: number; calibrated: number }
type HistPoint = { ts: string; n: number; brier_rolling: number; logloss_rolling?: number }
type RelPoint = { bin: string; n: number; confidence: number; accuracy: number }
type CalData = {
  model: { trained: boolean; model_type?: string; ensemble_weight?: number; train_metrics?: { accuracy?: number; brier_score?: number; n_samples?: number; model?: string } }
  calibration: { status?: string; fitted?: boolean; n_cal?: number; min_required?: number; pre_brier?: number; post_brier?: number; pre_ece?: number; post_ece?: number; brier_improvement?: number; curve?: CurvePoint[] }
  reliability?: RelPoint[]
  drift?: { status?: string; n?: number; brier_rolling?: number; brier_baseline?: number; logloss_rolling?: number; logloss_baseline?: number; recommendation?: string }
  drift_history?: HistPoint[]
  model_file?: { exists?: boolean; updated_at?: string }
  error?: string
}

const fmt = (v: number | undefined | null, digits = 4) =>
  v == null ? '—' : Number(v).toFixed(digits)
const pct = (v: number | undefined | null, digits = 1) =>
  v == null ? '—' : `${(Number(v) * 100).toFixed(digits)}%`

function CalChart({ points, line, title, subtitle }: {
  points: { x: number; y: number }[]
  line?: { x: number; y: number }[]
  title: string
  subtitle: string
}) {
  const W = 340, H = 280, PAD = 40
  const x = (v: number) => PAD + Math.max(0, Math.min(1, v)) * (W - 2 * PAD)
  const y = (v: number) => H - PAD - Math.max(0, Math.min(1, v)) * (H - 2 * PAD)
  const grid = [0, 0.25, 0.5, 0.75, 1]
  return (
    <div>
      <h3 className="font-bold text-lg mb-1">{title}</h3>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto bg-gray-900 rounded-lg">
        {grid.map(g => (
          <g key={g}>
            <line x1={x(g)} y1={y(0)} x2={x(g)} y2={y(1)} stroke="#374151" strokeWidth={1} />
            <line x1={x(0)} y1={y(g)} x2={x(1)} y2={y(g)} stroke="#374151" strokeWidth={1} />
            <text x={x(g)} y={H - PAD + 16} textAnchor="middle" fill="#9ca3af" fontSize="10">{g}</text>
            <text x={PAD - 6} y={y(g) + 3} textAnchor="end" fill="#9ca3af" fontSize="10">{g}</text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} stroke="#6b7280" strokeWidth={1.5} strokeDasharray="4 3" />
        {line && line.length > 1 && (
          <polyline
            points={line.map(p => `${x(p.x)},${y(p.y)}`).join(' ')}
            fill="none" stroke="#34d399" strokeWidth={2.5} strokeLinejoin="round"
          />
        )}
        {points.map((p, i) => (
          <circle key={i} cx={x(p.x)} cy={y(p.y)} r={4.5} fill="#f59e0b" stroke="#111827" strokeWidth={1} />
        ))}
        <text x={W / 2} y={H - 4} textAnchor="middle" fill="#6b7280" fontSize="11">probabilità predetta</text>
        <text x={12} y={H / 2} textAnchor="middle" fill="#6b7280" fontSize="11" transform={`rotate(-90 12 ${H / 2})`}>frequenza osservata</text>
      </svg>
      <p className="text-gray-400 text-xs mt-2">{subtitle}</p>
    </div>
  )
}

function TimeChart({ history, baseline, threshold }: {
  history: HistPoint[]
  baseline?: number
  threshold?: number
}) {
  const W = 700, H = 280, PAD = 44
  const vals = history.map(h => Number(h.brier_rolling))
  const yMax = Math.max(0.20, ...vals, baseline || 0, threshold || 0) * 1.15
  const x = (i: number) => {
    if (history.length <= 1) return W / 2
    return PAD + (i / (history.length - 1)) * (W - 2 * PAD)
  }
  const y = (v: number) => H - PAD - (Math.max(0, v) / yMax) * (H - 2 * PAD)
  const path = history.map((h, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(Number(h.brier_rolling)).toFixed(1)}`).join(' ')
  const ticks = [0, 1, 2, 3, 4].map(i => yMax * i / 4)
  const labelIdx = history.length <= 1 ? [0] : [0, Math.floor((history.length - 1) / 2), history.length - 1]
  const fmtDate = (ts: string) => {
    const d = new Date(ts)
    return isNaN(d.getTime()) ? ts : d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit' })
  }
  return (
    <div>
      <h3 className="font-bold text-lg mb-1">📈 Andamento drift nel tempo</h3>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto bg-gray-900 rounded-lg">
        {ticks.map(t => (
          <g key={t}>
            <line x1={PAD} y1={y(t)} x2={W - PAD} y2={y(t)} stroke="#374151" strokeWidth={1} />
            <text x={PAD - 6} y={y(t) + 3} textAnchor="end" fill="#9ca3af" fontSize="10">{t.toFixed(2)}</text>
          </g>
        ))}
        {baseline != null && (
          <line x1={PAD} y1={y(baseline)} x2={W - PAD} y2={y(baseline)} stroke="#34d399" strokeWidth={1.5} strokeDasharray="6 4" />
        )}
        {threshold != null && (
          <line x1={PAD} y1={y(threshold)} x2={W - PAD} y2={y(threshold)} stroke="#ef4444" strokeWidth={1.5} strokeDasharray="2 4" />
        )}
        {history.length > 1 && (
          <path d={path} fill="none" stroke="#f59e0b" strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
        )}
        {history.map((h, i) => (
          <circle key={i} cx={x(i)} cy={y(Number(h.brier_rolling))} r={3.5} fill="#fbbf24" stroke="#111827" strokeWidth={1} />
        ))}
        {labelIdx.map(i => (
          <text key={i} x={x(i)} y={H - PAD + 16} textAnchor="middle" fill="#9ca3af" fontSize="10">{fmtDate(history[i].ts)}</text>
        ))}
        <text x={12} y={H / 2} textAnchor="middle" fill="#6b7280" fontSize="11" transform={`rotate(-90 12 ${H / 2})`}>Brier rolling</text>
      </svg>
      <div className="flex gap-5 text-xs text-gray-400 mt-2 flex-wrap">
        <span><span className="inline-block w-4 h-0.5 bg-amber-500 align-middle mr-1" />Brier rolling (finestra {history[0]?.n ?? '—'}→{history[history.length - 1]?.n ?? '—'} chiusure)</span>
        {baseline != null && <span><span className="inline-block w-4 h-0.5 bg-emerald-400 align-middle mr-1" />baseline {baseline.toFixed(4)}</span>}
        {threshold != null && <span><span className="inline-block w-4 h-0.5 bg-red-500 align-middle mr-1" />soglia drift {threshold.toFixed(4)} (1.30× baseline)</span>}
      </div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const meta = status === 'ok'
    ? { label: '✅ OK', cls: 'bg-emerald-900 text-emerald-200' }
    : status === 'drift'
      ? { label: '🚨 DRIFT', cls: 'bg-red-900 text-red-200' }
      : { label: '⏳ INSUFFICIENTE', cls: 'bg-yellow-900 text-yellow-200' }
  return <span className={`text-xs font-bold px-2 py-1 rounded ${meta.cls}`}>{meta.label}</span>
}

export default function Calibrazione() {
  const [data, setData] = useState<CalData | null>(null)
  const [usingDemo, setUsingDemo] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/calibration`)
        if (!res.ok) throw new Error('fallback')
        const j = await res.json()
        if (j.error) throw new Error(j.error)
        setData(j)
      } catch {
        setData(DEMO)
        setUsingDemo(true)
      }
    })()
  }, [])

  if (!data) {
    return <main className="p-6 max-w-5xl mx-auto text-gray-400">Caricamento dashboard calibrazione…</main>
  }

  const m = data.model || {}
  const c = data.calibration || {}
  const d = data.drift || {}
  const rel = (data.reliability || []).map(r => ({ x: r.confidence, y: r.accuracy }))
  const curve = (c.curve || []).map(p => ({ x: p.score, y: p.calibrated }))
  const history = data.drift_history || []
  const driftStatus = d.status || 'unknown'
  const baseline = d.brier_baseline != null ? Number(d.brier_baseline) : undefined
  const threshold = baseline != null ? baseline * 1.30 : undefined
  const brierGap = d.brier_rolling != null && d.brier_baseline != null
    ? Number(d.brier_rolling) - Number(d.brier_baseline) : null

  return (
    <main className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold">🎯 Dashboard calibrazione</h1>
        <Link href="/dashboard" className="text-sm text-blue-400 hover:underline">← Dashboard</Link>
      </div>
      <p className="text-gray-400 text-sm mb-6">
        Istantanea delle prestazioni del modello 1X2: drift rolling, calibrazione isotonica e curva di affidabilità.
      </p>
      {usingDemo && <p className="text-amber-400 text-sm mb-6">⚠️ Backend non raggiungibile — dati dimostrativi.</p>}

      {/* Stato drift */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className={`p-4 rounded-xl bg-gray-800 border-l-4 ${driftStatus === 'drift' ? 'border-red-500' : driftStatus === 'ok' ? 'border-emerald-500' : 'border-yellow-500'}`}>
          <div className="flex items-center justify-between">
            <span className="text-gray-400 text-sm">Drift modello</span>
            <StatusBadge status={driftStatus} />
          </div>
          <div className="text-2xl font-bold mt-2">{d.n ?? 0} chiusure</div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-orange-500">
          <div className="text-gray-400 text-sm">Brier rolling</div>
          <div className="text-2xl font-bold">{fmt(d.brier_rolling)}</div>
          <div className="text-xs text-gray-500">baseline {fmt(d.brier_baseline)} {brierGap != null && <span className={brierGap > 0 ? 'text-red-400' : 'text-emerald-400'}>({brierGap >= 0 ? '+' : ''}{brierGap.toFixed(4)})</span>}</div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-purple-500">
          <div className="text-gray-400 text-sm">LogLoss rolling</div>
          <div className="text-2xl font-bold">{fmt(d.logloss_rolling)}</div>
          <div className="text-xs text-gray-500">baseline {fmt(d.logloss_baseline)}</div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-blue-500">
          <div className="text-gray-400 text-sm">Brier training</div>
          <div className="text-2xl font-bold">{fmt(m.train_metrics?.brier_score)}</div>
          <div className="text-xs text-gray-500">acc {pct(m.train_metrics?.accuracy)} · {m.train_metrics?.model || m.model_type || '—'}</div>
        </div>
      </div>

      {/* Andamento drift nel tempo */}
      {history.length > 0 ? (
        <div className="p-4 rounded-xl bg-gray-800 mb-6">
          <TimeChart history={history} baseline={baseline} threshold={threshold} />
        </div>
      ) : (
        <div className="p-4 rounded-xl bg-gray-800 mb-6 text-gray-400 text-sm">
          ⏳ Serie temporale drift non disponibile: servono almeno {d.n ?? '—'} previsioni chiuse (ne servono 15).
          I punti compariranno man mano che il ledger cresce.
        </div>
      )}

      {/* Calibrazione isotonica */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="p-4 rounded-xl bg-gray-800 border-l-4 border-emerald-500 md:col-span-1">
          <div className="text-gray-400 text-sm">Calibrazione isotonica</div>
          <div className="text-2xl font-bold mt-1">{c.fitted ? '✅ ATTIVA' : c.status === 'skipped' ? '⏸ IN ATTESA' : '❌ NON FIT'}</div>
          <div className="text-xs text-gray-500 mt-2">
            {c.fitted
              ? <>Fit su {c.n_cal ?? '—'} campioni OOF (≥ {c.min_required ?? 50} per attivarsi)</>
              : <>Soglia: {c.min_required ?? 50} campioni chiusi</>}
          </div>
          <div className="mt-3 text-sm space-y-1">
            <div className="flex justify-between"><span className="text-gray-400">Brier pre → post</span><b>{fmt(c.pre_brier)} → {fmt(c.post_brier)}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">ECE pre → post</span><b>{fmt(c.pre_ece)} → {fmt(c.post_ece)}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Miglioramento</span><b className="text-emerald-400">{c.brier_improvement != null ? `+${fmt(c.brier_improvement)}` : '—'}</b></div>
          </div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800 md:col-span-2">
          <CalChart
            title="Curva isotonica (score → prob. calibrata)"
            subtitle="Linea verde = mappa isotonica PAVA: porta gli score grezzi del ML sulle frequenze empiriche. La diagonale tratteggiata è la calibrazione perfetta."
            points={curve}
            line={curve}
          />
        </div>
      </div>

      {/* Reliability diagram */}
      <div className="p-4 rounded-xl bg-gray-800 mb-6">
        <CalChart
          title="Reliability diagram (previsioni chiuse)"
          subtitle={`Confidenza media per bin vs frequenza di vittoria osservata (${rel.length} bin, ${d.n ?? '—'} previsioni chiuse). Sotto la diagonale = overconfidence.`}
          points={rel}
        />
      </div>

      {/* Dettagli modello + monitoraggio */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="p-4 rounded-xl bg-gray-800">
          <h3 className="font-bold text-lg mb-2">🧠 Modello</h3>
          <div className="text-sm space-y-1">
            <div className="flex justify-between"><span className="text-gray-400">Stato</span><b>{m.trained ? 'addestrato' : 'non addestrato'}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Tipo</span><b>{m.model_type || '—'}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Peso ML nell&apos;ensemble</span><b>{pct(m.ensemble_weight)}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Campioni training</span><b>{m.train_metrics?.n_samples ?? '—'}</b></div>
            <div className="flex justify-between"><span className="text-gray-400">File modello</span><b className="text-xs">{data.model_file?.updated_at ? new Date(data.model_file.updated_at).toLocaleString('it-IT') : '—'}</b></div>
          </div>
        </div>
        <div className="p-4 rounded-xl bg-gray-800">
          <h3 className="font-bold text-lg mb-2">🛰 Monitoraggio in background</h3>
          <div className="text-sm space-y-1">
            <div className="flex justify-between"><span className="text-gray-400">Watchdog drift</span><b>ogni 6h</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Retraining</span><b>05:45 UTC + al boot</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Alert su drift</span><b>admin + iscritti (max 1/24h)</b></div>
            <div className="flex justify-between"><span className="text-gray-400">Endpoint remoto</span><b><code className="text-xs">GET /api/drift</code></b></div>
          </div>
          <p className="text-gray-500 text-xs mt-3">{d.recommendation || '—'}</p>
        </div>
      </div>
    </main>
  )
}