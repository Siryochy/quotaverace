export default function Dashboard() {
  return (
    <main className="p-6 max-w-5xl mx-auto">
      <h1 className="text-3xl font-bold mb-8">📊 Dashboard QuotaVerace</h1>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <Card title="💰 Bankroll" value="€500.00" color="emerald" />
        <Card title="📈 ROI 30gg" value="+12.4%" color="blue" />
        <Card title="🎯 Segnali Oggi" value="3" color="purple" />
      </div>
      <div className="bg-gray-800 rounded-xl p-6">
        <h2 className="text-xl font-bold mb-4">⚡ Ultime Value Bet</h2>
        <table className="w-full text-left">
          <thead><tr className="text-gray-400 border-b border-gray-600"><th className="pb-2">Evento</th><th className="pb-2">Esito</th><th className="pb-2">Quota</th><th className="pb-2">EV</th><th className="pb-2">Stake</th></tr></thead>
          <tbody>
            <tr className="border-b border-gray-700"><td className="py-3">Inter vs Napoli</td><td className="py-3">Over 2.5</td><td className="py-3">2.10</td><td className="py-3 text-emerald-400 font-bold">+15.5%</td><td className="py-3">€42.50</td></tr>
            <tr className="border-b border-gray-700"><td className="py-3">Roma vs Milan</td><td className="py-3">1</td><td className="py-3">2.40</td><td className="py-3 text-emerald-400 font-bold">+8.0%</td><td className="py-3">€25.00</td></tr>
          </tbody>
        </table>
      </div>
    </main>
  )
}
function Card({ title, value, color }: { title: string; value: string; color: string }) {
  const bc = color === 'emerald' ? 'border-emerald-500' : color === 'blue' ? 'border-blue-500' : 'border-purple-500'
  return <div className={`p-6 rounded-xl bg-gray-800 border-l-4 ${bc} shadow-lg`}><div className="text-gray-400 text-sm mb-1">{title}</div><div className="text-3xl font-bold">{value}</div></div>
}
