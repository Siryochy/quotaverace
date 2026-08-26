export default function Storico() {
  const signals = [
    { evento: 'Inter vs Napoli', esito: 'Over 2.5', quota: 2.10, ev: '+15.5%', risultato: '⏳', profit: null },
    { evento: 'Juventus vs Atalanta', esito: '1', quota: 2.20, ev: '+5.6%', risultato: '✅', profit: '+1.20u' },
    { evento: 'Roma vs Milan', esito: 'X', quota: 3.20, ev: '-4.0%', risultato: '❌', profit: '-1.00u' },
  ]
  return (
    <main className="p-6 max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">📜 Storico Segnali</h1>
      <div className="bg-gray-800 rounded-xl overflow-hidden">
        <table className="w-full text-left">
          <thead className="bg-gray-700"><tr><th className="p-4">Evento</th><th className="p-4">Esito</th><th className="p-4">Quota</th><th className="p-4">EV</th><th className="p-4">Risultato</th><th className="p-4">Profitto</th></tr></thead>
          <tbody>
            {signals.map((s, i) => (
              <tr key={i} className="border-b border-gray-700 hover:bg-gray-700/50 transition">
                <td className="p-4">{s.evento}</td><td className="p-4">{s.esito}</td><td className="p-4">{s.quota.toFixed(2)}</td>
                <td className={`p-4 font-bold ${s.ev.startsWith('+') ? 'text-emerald-400' : 'text-red-400'}`}>{s.ev}</td>
                <td className="p-4 text-lg">{s.risultato}</td><td className="p-4">{s.profit || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-6 bg-gray-800 rounded-xl p-6">
        <h2 className="text-xl font-bold mb-4">📈 Riepilogo 30 giorni</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label="Segnali" value="24" /><Stat label="Vinti" value="14" /><Stat label="Persi" value="10" /><Stat label="ROI" value="+8.3%" highlight />
        </div>
      </div>
    </main>
  )
}
function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return <div className={`p-4 rounded-lg text-center ${highlight ? 'bg-emerald-900/30 border border-emerald-500/30' : 'bg-gray-700'}`}><div className="text-gray-400 text-sm">{label}</div><div className={`text-2xl font-bold ${highlight ? 'text-emerald-400' : ''}`}>{value}</div></div>
}
