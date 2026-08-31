export const metadata = { title: 'QuotaVerace Dashboard' }
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body className="bg-gray-900 text-white min-h-screen">
        <nav className="p-4 border-b border-gray-700 flex gap-5 items-center overflow-x-auto">
          <span className="font-bold text-emerald-400 text-xl">⚽ QuotaVerace</span>
          <a href="/dashboard" className="hover:text-emerald-400 transition whitespace-nowrap">Dashboard</a>
          <a href="/cassa" className="hover:text-emerald-400 transition whitespace-nowrap font-medium text-emerald-300">💰 Cassa</a>
          <a href="/calendario" className="hover:text-emerald-400 transition whitespace-nowrap">Calendario</a>
          <a href="/value" className="hover:text-emerald-400 transition whitespace-nowrap">Value</a>
          <a href="/schedina" className="hover:text-emerald-400 transition whitespace-nowrap">Schedina</a>
          <a href="/calcola" className="hover:text-emerald-400 transition whitespace-nowrap">Calcola</a>
          <a href="/backtest" className="hover:text-emerald-400 transition whitespace-nowrap">Backtest</a>
          <a href="/storico" className="hover:text-emerald-400 transition whitespace-nowrap">Storico</a>
        </nav>
        {children}
      </body>
    </html>
  )
}
