import { useEffect, useState } from "react"
import { Server, Activity, ShieldAlert, ActivityIcon, FileJson, Cpu, Shield, Globe, ImageIcon, Paperclip, Flame, Database } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"

type AccountRow = {
  email: string
  status: string
  inflight: number
  max_inflight: number
  consecutive_failures: number
  rate_limit_strikes: number
  last_request_finished: number
}

type Status = {
  accounts?: {
    total?: number
    valid?: number
    rate_limited?: number
    invalid?: number
    in_use?: number
    global_in_use?: number
    waiting?: number
    max_inflight_per_account?: number
    max_queue_size?: number
  }
  per_account?: AccountRow[]
  chat_id_pool?: {
    total_cached?: number
    target_per_account?: number
    ttl_seconds?: number
    per_account?: Record<string, number>
  } | null
  runtime?: { asyncio_running_tasks?: number }
}

export default function Dashboard() {
  const [status, setStatus] = useState<Status | null>(null)
  const [errOnce, setErrOnce] = useState(false)

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/admin/status`, { headers: getAuthHeader() })
        if (!res.ok) throw new Error("Unauthorized")
        const data = await res.json()
        setStatus(data)
      } catch {
        if (!errOnce) {
          toast.error("Failed to fetch status, please check your session key in Settings.")
          setErrOnce(true)
        }
      }
    }
    fetchStatus()
    const timer = setInterval(fetchStatus, 3000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const acc = status?.accounts || {}
  const pool = status?.chat_id_pool
  const rows = status?.per_account || []

  return (
    <div className="space-y-8 max-w-5xl relative">
      <div className="relative z-10">
        <div className="absolute -top-10 -left-10 w-40 h-40 bg-primary/20 blur-[100px] rounded-full pointer-events-none" />
        <h2 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-foreground to-foreground/60 bg-clip-text text-transparent">Dashboard</h2>
        <p className="text-muted-foreground mt-2 text-lg">Global concurrency monitoring and Qwen account pool overview (auto-refreshes every 3 seconds).</p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4 relative z-10">
        <StatCard icon={<Server className="h-5 w-5 text-primary" />} title="Available Accounts" value={String(acc.valid ?? 0)} accent="primary" sub={`Total ${acc.total ?? 0}`} />
        <StatCard icon={<Activity className="h-5 w-5 text-blue-400" />} title="Current Concurrency" value={String(acc.in_use ?? 0)} accent="blue" sub={`Global limit ${acc.global_in_use ?? 0}`} />
        <StatCard icon={<ShieldAlert className="h-5 w-5 text-destructive" />} title="Queued Requests" value={String(acc.waiting ?? 0)} accent="destructive" sub={`Queue limit ${acc.max_queue_size ?? 0}`} />
        <StatCard icon={<ActivityIcon className="h-5 w-5 text-orange-400" />} title="Rate limited / Invalid" value={`${acc.rate_limited ?? 0} / ${acc.invalid ?? 0}`} accent="orange" />
      </div>

      <div className="grid gap-6 md:grid-cols-2 relative z-10">
        <StatCard icon={<Flame className="h-5 w-5 text-rose-400" />} title="Chat_ID Warmup Pool" value={String(pool?.total_cached ?? 0)} accent="rose" sub={pool ? `Target ${pool.target_per_account}/account · TTL ${Math.round((pool.ttl_seconds || 0) / 60)} min` : "Not enabled"} />
        <StatCard icon={<Database className="h-5 w-5 text-cyan-400" />} title="Async Tasks" value={String(status?.runtime?.asyncio_running_tasks ?? 0)} accent="cyan" sub="asyncio active task count" />
      </div>

      {rows.length > 0 && (
        <div className="rounded-2xl border border-border/50 bg-card/30 backdrop-blur-xl shadow-2xl relative overflow-hidden">
          <div className="absolute inset-0 bg-gradient-to-b from-black/[0.02] dark:from-white/[0.02] to-transparent pointer-events-none" />
          <div className="flex flex-col space-y-2 p-6 border-b border-border/50 bg-muted/10 relative z-10">
            <h3 className="font-extrabold text-xl tracking-tight flex items-center gap-3">
              <span className="bg-primary w-2 h-6 rounded-full shadow-[0_0_10px_rgba(168,85,247,0.5)]"></span>
              Account Concurrency Details
            </h3>
          </div>
          <div className="overflow-x-auto relative z-10">
            <table className="w-full text-sm">
              <thead className="bg-muted/20 text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="text-left px-6 py-3 font-semibold">Email</th>
                  <th className="text-left px-4 py-3 font-semibold">Status</th>
                  <th className="text-right px-4 py-3 font-semibold">Inflight</th>
                  <th className="text-right px-4 py-3 font-semibold">Warmup chat_id</th>
                  <th className="text-right px-4 py-3 font-semibold">Consecutive Failures</th>
                  <th className="text-right px-4 py-3 font-semibold">Rate limit Strikes</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {rows.map(r => {
                  const warmSize = pool?.per_account?.[r.email] ?? 0
                  const badge = r.status === "valid" ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30"
                              : r.status === "rate_limited" ? "bg-orange-500/15 text-orange-300 ring-orange-500/30"
                              : "bg-red-500/15 text-red-300 ring-red-500/30"
                  return (
                    <tr key={r.email} className="hover:bg-muted/10 transition-colors">
                      <td className="px-6 py-3 font-mono text-xs text-foreground/80 break-all">{r.email}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-bold ring-1 ${badge}`}>{r.status}</span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono">
                        {r.inflight}<span className="text-muted-foreground">/{r.max_inflight}</span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono">
                        <span className={warmSize === 0 ? "text-muted-foreground" : "text-rose-400 font-semibold"}>{warmSize}</span>
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-xs">{r.consecutive_failures}</td>
                      <td className="px-4 py-3 text-right font-mono text-xs">{r.rate_limit_strikes}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="rounded-2xl border border-border/50 bg-card/30 backdrop-blur-xl shadow-2xl relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-b from-black/[0.02] dark:from-white/[0.02] to-transparent pointer-events-none" />
        <div className="flex flex-col space-y-2 p-8 border-b border-border/50 bg-muted/10 relative z-10">
          <h3 className="font-extrabold text-2xl tracking-tight flex items-center gap-3">
            <span className="bg-primary w-2 h-8 rounded-full shadow-[0_0_10px_rgba(168,85,247,0.5)]"></span>
            API Endpoint Pool
          </h3>
          <p className="text-base text-muted-foreground ml-5">Call entry points compatible with mainstream AI protocols, no authentication required by default or accessible via API Key.</p>
        </div>
        <div className="p-0 relative z-10">
          <div className="divide-y divide-border/50 text-sm">
            <EndpointRow icon={<FileJson className="h-5 w-5 text-emerald-500 dark:text-emerald-400" />} iconBg="bg-emerald-500/10" path="POST /v1/chat/completions" tag="OpenAI" tagColor="emerald" />
            <EndpointRow icon={<Cpu className="h-5 w-5 text-blue-500 dark:text-blue-400" />} iconBg="bg-blue-500/10" path="POST /v1/messages" tag="Anthropic" tagColor="blue" />
            <EndpointRow icon={<Globe className="h-5 w-5 text-yellow-600 dark:text-yellow-400" />} iconBg="bg-yellow-500/10" path="POST /v1/models/gemini-pro:generateContent" tag="Gemini" tagColor="yellow" />
            <EndpointRow icon={<ImageIcon className="h-5 w-5 text-purple-500 dark:text-purple-400" />} iconBg="bg-purple-500/10" path="POST /v1/images/generations" tag="Image Gen" tagColor="purple" />
            <EndpointRow icon={<Paperclip className="h-5 w-5 text-cyan-500 dark:text-cyan-400" />} iconBg="bg-cyan-500/10" path="POST /v1/files" tag="Files" tagColor="cyan" />
            <EndpointRow icon={<Shield className="h-5 w-5 text-slate-600 dark:text-slate-400" />} iconBg="bg-slate-500/10" path="GET /" tag="健康检查" tagColor="slate" />
          </div>
        </div>
      </div>
    </div>
  )
}

function StatCard({ icon, title, value, accent, sub }: { icon: React.ReactNode; title: string; value: string; accent: string; sub?: string }) {
  const shadowMap: Record<string, string> = {
    primary: "hover:shadow-primary/5",
    blue: "hover:shadow-blue-500/5",
    destructive: "hover:shadow-destructive/10",
    orange: "hover:shadow-orange-500/5",
    rose: "hover:shadow-rose-500/5",
    cyan: "hover:shadow-cyan-500/5",
  }
  const gradMap: Record<string, string> = {
    primary: "from-primary/10",
    blue: "from-blue-500/10",
    destructive: "from-destructive/10",
    orange: "from-orange-500/10",
    rose: "from-rose-500/10",
    cyan: "from-cyan-500/10",
  }
  return (
    <div className={`group rounded-2xl border border-border/50 bg-card/40 backdrop-blur-md shadow-xl ${shadowMap[accent]} transition-all duration-500 overflow-hidden relative`}>
      <div className={`absolute inset-0 bg-gradient-to-br ${gradMap[accent]} to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500`} />
      <div className="p-6 relative z-10">
        <div className="flex flex-row items-center justify-between space-y-0 pb-4">
          <h3 className="tracking-tight text-sm font-semibold text-foreground/80 uppercase">{title}</h3>
          <div className="p-2 bg-primary/10 rounded-lg">{icon}</div>
        </div>
        <div className="text-4xl font-black bg-gradient-to-r from-foreground to-foreground/70 bg-clip-text text-transparent">
          {value}
        </div>
        {sub ? <div className="text-xs text-muted-foreground mt-2">{sub}</div> : null}
      </div>
    </div>
  )
}

function EndpointRow({ icon, iconBg, path, tag, tagColor }: { icon: React.ReactNode; iconBg: string; path: string; tag: string; tagColor: string }) {
  const tagClass = `bg-${tagColor}-500/10 text-${tagColor}-600 dark:bg-${tagColor}-500/20 dark:text-${tagColor}-300 ring-1 ring-${tagColor}-500/20 dark:ring-${tagColor}-500/30`
  return (
    <div className="flex justify-between items-center px-8 py-5 hover:bg-black/5 dark:hover:bg-white/[0.02] transition-colors">
      <div className="flex items-center gap-4">
        <div className={`p-2 rounded-md ${iconBg}`}>{icon}</div>
        <div className="font-semibold text-foreground/80">{path}</div>
      </div>
      <span className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-bold ${tagClass}`}>{tag}</span>
    </div>
  )
}
