export const metadata = { title: 'QuotaVerace Dashboard' }
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body className="bg-gray-900 text-white min-h-screen">
        <nav className="p-4 border-b border-gray-700 flex gap-6 items-center">
          <span className="font-bold text-emerald-400 text-xl">⚽ QuotaVerace</span>
          <a href="/dashboard" className="hover:text-emerald-400 transition">Dashboard</a>
          <a href="/calcola" className="hover:text-emerald-400 transition">Calcola</a>
          <a href="/storico" className="hover:text-emerald-400 transition">Storico</a>
        </nav>
        {children}
      </body>
    </html>
  )
}
