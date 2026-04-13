import { useEffect, useState } from "react"
import { Server, Activity, ShieldAlert, ActivityIcon, FileJson, Cpu, Shield, Globe, ImageIcon } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null)

  useEffect(() => {
    fetch(`${API_BASE}/api/admin/status`, { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => setStatus(data))
      .catch(() => toast.error("状态获取失败，请在「系统设置」检查您的当前会话 Key。"))
  }, [])

  return (
    <div className="space-y-8 max-w-5xl relative">
      <div className="relative z-10">
        <div className="absolute -top-10 -left-10 w-40 h-40 bg-primary/20 blur-[100px] rounded-full pointer-events-none" />
        <h2 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-foreground to-foreground/60 bg-clip-text text-transparent">运行状态</h2>
        <p className="text-muted-foreground mt-2 text-lg">全局并发监控与千问账号池概览。</p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4 relative z-10">
        <div className="group rounded-2xl border border-border/50 bg-card/40 backdrop-blur-md shadow-xl hover:shadow-primary/5 transition-all duration-500 overflow-hidden relative">
          <div className="absolute inset-0 bg-gradient-to-br from-primary/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
          <div className="p-6 relative z-10">
            <div className="flex flex-row items-center justify-between space-y-0 pb-4">
              <h3 className="tracking-tight text-sm font-semibold text-foreground/80 uppercase">可用账号</h3>
              <div className="p-2 bg-primary/10 rounded-lg"><Server className="h-5 w-5 text-primary" /></div>
            </div>
            <div className="text-4xl font-black bg-gradient-to-r from-foreground to-foreground/70 bg-clip-text text-transparent">
              {status?.accounts?.valid || 0}
            </div>
          </div>
        </div>

        <div className="group rounded-2xl border border-border/50 bg-card/40 backdrop-blur-md shadow-xl hover:shadow-blue-500/5 transition-all duration-500 overflow-hidden relative">
          <div className="absolute inset-0 bg-gradient-to-br from-blue-500/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
          <div className="p-6 relative z-10">
            <div className="flex flex-row items-center justify-between space-y-0 pb-4">
              <h3 className="tracking-tight text-sm font-semibold text-foreground/80 uppercase">当前并发引擎</h3>
              <div className="p-2 bg-blue-500/10 rounded-lg"><Activity className="h-5 w-5 text-blue-400" /></div>
            </div>
            <div className="text-4xl font-black bg-gradient-to-r from-foreground to-foreground/70 bg-clip-text text-transparent flex items-baseline gap-2">
              {status?.browser_engine?.pool_size || 0} <span className="text-lg font-bold text-muted-foreground">Pages</span>
            </div>
          </div>
        </div>

        <div className="group rounded-2xl border border-destructive/20 bg-card/40 backdrop-blur-md shadow-xl hover:shadow-destructive/10 transition-all duration-500 overflow-hidden relative">
          <div className="absolute inset-0 bg-gradient-to-br from-destructive/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
          <div className="p-6 relative z-10">
            <div className="flex flex-row items-center justify-between space-y-0 pb-4">
              <h3 className="tracking-tight text-sm font-semibold text-destructive uppercase">排队请求数</h3>
              <div className="p-2 bg-destructive/10 rounded-lg"><ShieldAlert className="h-5 w-5 text-destructive" /></div>
            </div>
            <div className="text-4xl font-black text-destructive drop-shadow-[0_0_15px_rgba(239,68,68,0.3)]">
              {status?.browser_engine?.queue || 0}
            </div>
          </div>
        </div>

        <div className="group rounded-2xl border border-border/50 bg-card/40 backdrop-blur-md shadow-xl hover:shadow-orange-500/5 transition-all duration-500 overflow-hidden relative">
          <div className="absolute inset-0 bg-gradient-to-br from-orange-500/10 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
          <div className="p-6 relative z-10">
            <div className="flex flex-row items-center justify-between space-y-0 pb-4">
              <h3 className="tracking-tight text-sm font-semibold text-foreground/80 uppercase">限流号/失效号</h3>
              <div className="p-2 bg-orange-500/10 rounded-lg"><ActivityIcon className="h-5 w-5 text-orange-400" /></div>
            </div>
            <div className="text-4xl font-black bg-gradient-to-r from-foreground to-foreground/70 bg-clip-text text-transparent">
              {status?.accounts?.rate_limited || 0} <span className="text-muted-foreground font-light mx-1">/</span> {status?.accounts?.invalid || 0}
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-2xl border border-border/50 bg-card/30 backdrop-blur-xl shadow-2xl relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-black/[0.02] dark:from-white/[0.02] to-transparent pointer-events-none" />
        <div className="flex flex-col space-y-2 p-8 border-b border-border/50 bg-muted/10 relative z-10">
          <h3 className="font-extrabold text-2xl tracking-tight flex items-center gap-3">
            <span className="bg-primary w-2 h-8 rounded-full shadow-[0_0_10px_rgba(168,85,247,0.5)]"></span>
            API 接口池
          </h3>
          <p className="text-base text-muted-foreground ml-5">兼容主流 AI 协议的调用入口，默认无需认证，或通过 API Key 访问。</p>
        </div>
        <div className="p-0 relative z-10">
          <div className="divide-y divide-border/50 text-sm">
            <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
              <div className="flex items-center gap-4">
                <div className="p-2 rounded-md bg-emerald-500/10"><FileJson className="h-5 w-5 text-emerald-500 dark:text-emerald-400" /></div>
                <div className="font-semibold text-foreground/80">POST /v1/chat/completions</div>
              </div>
              <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-bold bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-300 ring-1 ring-emerald-500/20 dark:ring-emerald-500/30">OpenAI</span>
            </div>
            <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
              <div className="flex items-center gap-4">
                <div className="p-2 rounded-md bg-blue-500/10"><Cpu className="h-5 w-5 text-blue-500 dark:text-blue-400" /></div>
                <div className="font-semibold text-foreground/80">POST /v1/messages</div>
              </div>
              <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-bold bg-blue-500/10 text-blue-600 dark:bg-blue-500/20 dark:text-blue-300 ring-1 ring-blue-500/20 dark:ring-blue-500/30">Anthropic</span>
            </div>
            <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
              <div className="flex items-center gap-4">
                <div className="p-2 rounded-md bg-yellow-500/10"><Globe className="h-5 w-5 text-yellow-600 dark:text-yellow-400" /></div>
                <div className="font-semibold text-foreground/80">POST /v1/models/gemini-pro:generateContent</div>
              </div>
              <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-bold bg-yellow-500/10 text-yellow-600 dark:bg-yellow-500/20 dark:text-yellow-300 ring-1 ring-yellow-500/20 dark:ring-yellow-500/30">Gemini</span>
            </div>
            <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
              <div className="flex items-center gap-4">
                <div className="p-2 rounded-md bg-purple-500/10"><ImageIcon className="h-5 w-5 text-purple-500 dark:text-purple-400" /></div>
                <div className="font-semibold text-foreground/80">POST /v1/images/generations</div>
              </div>
              <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-bold bg-purple-500/10 text-purple-600 dark:bg-purple-500/20 dark:text-purple-300 ring-1 ring-purple-500/20 dark:ring-purple-500/30">Image Gen</span>
            </div>
            <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
              <div className="flex items-center gap-4">
                <div className="p-2 rounded-md bg-slate-500/10"><Shield className="h-5 w-5 text-slate-600 dark:text-slate-400" /></div>
                <div className="font-semibold text-foreground/80">GET /</div>
              </div>
              <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-bold bg-slate-500/10 text-slate-600 dark:bg-slate-500/20 dark:text-slate-300 ring-1 ring-slate-500/20 dark:ring-slate-500/30">健康检查</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
