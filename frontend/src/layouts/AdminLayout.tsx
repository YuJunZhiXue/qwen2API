import { Outlet, Link, useLocation } from "react-router-dom"
import { Activity, Key, Settings, LayoutDashboard, MessageSquare, Menu, X, Image, Languages } from "lucide-react"
import { useState } from "react"
import { useTranslation } from "react-i18next"

export default function AdminLayout() {
  const loc = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const { t, i18n } = useTranslation()

  const navs = [
    { key: "dashboard", path: "/", icon: LayoutDashboard },
    { key: "accounts", path: "/accounts", icon: Activity },
    { key: "apiKey", path: "/tokens", icon: Key },
    { key: "test", path: "/test", icon: MessageSquare },
    { key: "images", path: "/images", icon: Image },
    { key: "settings", path: "/settings", icon: Settings },
  ]

  const currentLang = (i18n.resolvedLanguage || i18n.language || "en").startsWith("pt") ? "pt-BR" : "en"

  const handleLangChange = (lng: string) => {
    i18n.changeLanguage(lng)
  }

  return (
    <div className="flex min-h-screen w-full bg-background text-foreground transition-colors duration-300">
      {/* Mobile sidebar backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 bg-black/20 dark:bg-black/50 z-40 md:hidden backdrop-blur-sm transition-opacity"
          onClick={() => setMobileOpen(false)}
        />
      )}

      <aside className={`fixed md:static inset-y-0 left-0 w-64 flex-col border-r border-border/40 bg-card/90 md:bg-card/50 backdrop-blur-xl flex z-50 shadow-2xl shadow-black/5 dark:shadow-black/50 transition-transform duration-300 ${
        mobileOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
      }`}>
        <div className="h-16 flex items-center justify-between px-6 border-b border-border/40">
          <div className="font-extrabold text-xl tracking-tight bg-gradient-to-br from-indigo-500 to-purple-500 bg-clip-text text-transparent">qwen2API</div>
          <button className="md:hidden text-muted-foreground hover:text-foreground transition-colors" onClick={() => setMobileOpen(false)}>
            <X className="h-5 w-5" />
          </button>
        </div>
        <nav className="flex-1 space-y-2 p-4">
          {navs.map(n => {
            const active = loc.pathname === n.path
            return (
              <Link
                key={n.path}
                to={n.path}
                onClick={() => setMobileOpen(false)}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-300 ${
                  active
                  ? "bg-primary/10 text-primary shadow-[inset_0_1px_0_0_rgba(0,0,0,0.05)] dark:shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)] ring-1 ring-primary/20"
                  : "text-muted-foreground hover:bg-black/5 dark:hover:bg-white/5 hover:text-foreground"
                }`}
              >
                <n.icon className={`h-4 w-4 ${active ? "drop-shadow-[0_0_8px_rgba(168,85,247,0.5)]" : ""}`} />
                {t(`nav.${n.key}`)}
              </Link>
            )
          })}
        </nav>
        <div className="border-t border-border/40 p-4">
          <label className="flex items-center gap-2 text-xs text-muted-foreground mb-2">
            <Languages className="h-4 w-4" />
            <span className="font-semibold uppercase tracking-wider">Language</span>
          </label>
          <select
            value={currentLang}
            onChange={e => handleLangChange(e.target.value)}
            className="w-full h-9 rounded-md border border-input bg-background px-2 text-sm cursor-pointer"
          >
            <option value="en">{t("common.languageEN")}</option>
            <option value="pt-BR">{t("common.languagePT")}</option>
          </select>
        </div>
      </aside>

      <main className="flex-1 flex flex-col overflow-hidden relative">
        <header className="h-16 flex items-center justify-between px-6 border-b border-border/40 bg-card/80 backdrop-blur-xl md:hidden z-10 shadow-sm">
           <div className="font-extrabold text-lg bg-gradient-to-br from-indigo-500 to-purple-500 bg-clip-text text-transparent">qwen2API</div>
           <button className="text-muted-foreground hover:text-foreground transition-colors" onClick={() => setMobileOpen(true)}>
             <Menu className="h-6 w-6" />
           </button>
        </header>
        <div className="flex-1 p-6 md:p-8 overflow-y-auto overflow-x-hidden z-0">
          <div className="max-w-6xl mx-auto min-w-0 animate-fade-in-up">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
