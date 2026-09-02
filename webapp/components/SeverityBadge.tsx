// SeverityBadge — badge ad alta visibilità per alert di mercato (RLM/steam/crollo)
// e segnali value. URGENTE con bordo animato rosso, warning ambrato.
'use client'

export type Severity = 'urgent' | 'warning' | 'info' | 'value' | 'strong'

export const SEVERITY_STYLES: Record<Severity, { cls: string; label: string }> = {
  urgent: { cls: 'bg-red-600 text-white animate-pulse ring-2 ring-red-400', label: 'URGENTE' },
  warning: { cls: 'bg-amber-500 text-black ring-2 ring-amber-300', label: '⚠️ ATTENZIONE' },
  info: { cls: 'bg-gray-700 text-gray-200', label: 'INFO' },
  value: { cls: 'bg-emerald-500 text-white ring-2 ring-emerald-300', label: '✅ VALUE' },
  strong: { cls: 'bg-orange-500 text-white ring-2 ring-orange-300 animate-pulse', label: '🔥 STRONG VALUE' },
}

export default function SeverityBadge({ type }: { type: Severity }) {
  const s = SEVERITY_STYLES[type] || SEVERITY_STYLES.info
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-bold px-2.5 py-1 rounded-full ${s.cls}`}>
      {s.label}
    </span>
  )
}

// Mappa tipo alert -> stile visivo per le card (colori di bordo/etichetta).
export const ALERT_TYPE_META: Record<string, { emoji: string; label: string; border: string; badge: 'urgent' | 'warning' | 'info'; desc: string }> = {
  steam: { emoji: '🔥', label: 'STEAM', border: 'border-orange-500', badge: 'urgent', desc: 'Sharp money in entrata: movimento improvviso del mercato.' },
  crash: { emoji: '🚨', label: 'CROLLO QUOTA', border: 'border-red-500', badge: 'urgent', desc: 'Edge in erosione: esegui subito o scarta il segnale.' },
  rlm: { emoji: '⚠️', label: 'RLM', border: 'border-yellow-500', badge: 'warning', desc: 'Il prezzo si muove contro il pubblico: possibile money sharp.' },
}