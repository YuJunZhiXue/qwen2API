import { Outlet, Link, useLocation } from "react-router-dom"
import {
  Activity,
  Image,
  Key,
  LayoutDashboard,
  Menu,
  MessageSquare,
  Settings,
  Video,
  X,
} from "lucide-react"
import { useState, useEffect } from "react"
import LanguageSelector from "../components/LanguageSelector"
import { reapplyLanguageOnRouteChange } from "../lib/translate"

const navItems = [
  { name: "运行状态", path: "/", icon: LayoutDashboard },
  { name: "账号管理", path: "/accounts", icon: Activity },
  { name: "API Key", path: "/tokens", icon: Key },
  { name: "接口测试", path: "/test", icon: MessageSquare },
  { name: "图片生成", path: "/images", icon: Image },
  { name: "视频生成", path: "/videos", icon: Video },
  { name: "系统设置", path: "/settings", icon: Settings },
]

export default function AdminLayout() {
  const loc = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const activeItem = navItems.find(item => item.path === loc.pathname) || navItems[0]

  useEffect(() => {
    reapplyLanguageOnRouteChange()
  }, [loc.pathname])

  return (
    <div className="relative flex h-screen w-full overflow-hidden text-foreground">
      <div className="pointer-events-none fixed inset-0 -z-10 bg-[radial-gradient(circle_at_15%_10%,hsl(var(--card)/0.9),transparent_28rem),radial-gradient(circle_at_92%_12%,hsl(var(--accent)/0.34),transparent_24rem)]" />

      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/20 backdrop-blur-sm md:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex h-screen w-[16.5rem] flex-col border-r border-white/70 bg-card/92 shadow-[var(--shadow-lift)] backdrop-blur-2xl transition-transform duration-300 md:relative md:inset-auto md:translate-x-0 ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-20 items-center justify-between border-b border-border/45 px-6">
          <Link to="/" onClick={() => setMobileOpen(false)} className="group min-w-0">
            <div className="text-[11px] font-black uppercase tracking-[0.32em] text-muted-foreground">Go Gateway</div>
            <div className="mt-1 truncate text-2xl font-black tracking-tight transition group-hover:text-primary">qwen2API</div>
          </Link>
          <button
            type="button"
            className="rounded-full border border-white/70 bg-card/70 p-2 text-muted-foreground shadow-sm md:hidden"
            onClick={() => setMobileOpen(false)}
            aria-label="关闭导航"
          >
            <X className="size-5" />
          </button>
        </div>

        <nav className="flex-1 space-y-2 overflow-y-auto p-4">
          {navItems.map(item => {
            const active = loc.pathname === item.path
            return (
              <Link
                key={item.path}
                to={item.path}
                onClick={() => setMobileOpen(false)}
                className={`group flex items-center gap-3 whitespace-nowrap rounded-2xl border px-4 py-3 text-sm font-bold transition ${
                  active
                    ? "border-primary/10 bg-primary text-primary-foreground shadow-[0_18px_34px_-24px_hsl(var(--primary)/0.85)]"
                    : "border-transparent text-muted-foreground hover:border-white/75 hover:bg-card/75 hover:text-foreground hover:shadow-sm"
                }`}
              >
                <span
                  className={`grid size-9 shrink-0 place-items-center rounded-xl transition ${
                    active ? "bg-primary-foreground/12" : "bg-muted/45 group-hover:bg-accent/55"
                  }`}
                >
                  <item.icon className="size-4" />
                </span>
                <span className="truncate">{item.name}</span>
              </Link>
            )
          })}
        </nav>

        <div className="border-t border-border/45 p-4">
          <div className="rounded-[24px] border border-white/70 bg-muted/22 p-4 shadow-sm">
            <div className="flex items-center gap-2 text-xs font-black text-muted-foreground">
              <span className="size-2 rounded-full bg-accent" />
              Go 后端运行
            </div>
            <div className="mt-2 text-sm leading-6 text-muted-foreground">
              保持原版管理入口，前端只展示必要页面。
            </div>
          </div>
        </div>
      </aside>

      <main className="flex h-screen min-w-0 flex-1 flex-col overflow-hidden">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-white/70 bg-card/76 px-4 shadow-sm backdrop-blur-xl md:h-20 md:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="rounded-full border border-white/70 bg-card/70 p-2 text-muted-foreground shadow-sm md:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="打开导航"
            >
              <Menu className="size-5" />
            </button>
            <div className="min-w-0">
              <div className="text-[11px] font-black uppercase tracking-[0.24em] text-muted-foreground">Admin Console</div>
              <h1 className="truncate text-xl font-black tracking-tight md:text-2xl">{activeItem.name}</h1>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <LanguageSelector />
            <div className="hidden items-center gap-2 lg:flex">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/75 bg-card/70 px-3 py-1 text-xs font-bold text-muted-foreground shadow-sm">
                <span className="size-1.5 rounded-full bg-accent" />
                qwen2API
              </span>
            </div>
          </div>
        </header>

        <section className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-6">
          <div className="min-w-0 animate-fade-in-up">
            <Outlet />
          </div>
        </section>
      </main>
    </div>
  )
}
