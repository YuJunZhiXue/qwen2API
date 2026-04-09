import { useEffect, useState } from "react"
import { Server, Activity, ShieldAlert, ActivityIcon, FileJson, Cpu, Shield, Globe } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { toast } from "sonner"

export default function Dashboard() {
  const [status, setStatus] = useState<any>(null)

  useEffect(() => {
    fetch("/api/admin/status", { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => setStatus(data))
      .catch(() => toast.error("状态获取失败，请在「系统设置」检查您的当前会话 Key。"))
  }, [])

  return (
    <div className="space-y-8 max-w-5xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">运行状态</h2>
        <p className="text-muted-foreground">全局并发监控与千问账号池概览。</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
          <div className="flex flex-row items-center justify-between space-y-0 pb-2">
            <h3 className="tracking-tight text-sm font-medium">可用账号</h3>
            <Server className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="text-2xl font-bold">{status?.accounts?.valid || 0}</div>
        </div>

        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
          <div className="flex flex-row items-center justify-between space-y-0 pb-2">
            <h3 className="tracking-tight text-sm font-medium">当前并发引擎</h3>
            <Activity className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="text-2xl font-bold">{status?.browser_engine?.pool_size || 0} Pages</div>
        </div>

        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
          <div className="flex flex-row items-center justify-between space-y-0 pb-2">
            <h3 className="tracking-tight text-sm font-medium text-destructive">排队请求数</h3>
            <ShieldAlert className="h-4 w-4 text-destructive" />
          </div>
          <div className="text-2xl font-bold text-destructive">{status?.browser_engine?.queue || 0}</div>
        </div>

        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
          <div className="flex flex-row items-center justify-between space-y-0 pb-2">
            <h3 className="tracking-tight text-sm font-medium">限流号/失效号</h3>
            <ActivityIcon className="h-4 w-4 text-muted-foreground" />
          </div>
          <div className="text-2xl font-bold">{status?.accounts?.rate_limited || 0} / {status?.accounts?.invalid || 0}</div>
        </div>
      </div>

      <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
        <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
          <h3 className="font-semibold leading-none tracking-tight">API 接口</h3>
          <p className="text-sm text-muted-foreground">兼容主流 AI 协议的调用入口，默认无需认证，或通过 API Key 访问。</p>
        </div>
        <div className="p-0">
          <div className="divide-y text-sm">
            <div className="flex justify-between items-center px-6 py-4">
              <div className="flex items-center gap-3">
                <FileJson className="h-4 w-4 text-muted-foreground" />
                <code className="font-mono text-primary font-medium">POST /v1/chat/completions</code>
              </div>
              <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-green-100 text-green-800">OpenAI</span>
            </div>
            <div className="flex justify-between items-center px-6 py-4">
              <div className="flex items-center gap-3">
                <Cpu className="h-4 w-4 text-muted-foreground" />
                <code className="font-mono text-primary font-medium">POST /anthropic/v1/messages</code>
              </div>
              <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-blue-100 text-blue-800">Anthropic</span>
            </div>
            <div className="flex justify-between items-center px-6 py-4">
              <div className="flex items-center gap-3">
                <Globe className="h-4 w-4 text-muted-foreground" />
                <code className="font-mono text-primary font-medium">POST /v1beta/models/&#123;model&#125;:generateContent</code>
              </div>
              <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-yellow-100 text-yellow-800">Gemini</span>
            </div>
            <div className="flex justify-between items-center px-6 py-4">
              <div className="flex items-center gap-3">
                <Shield className="h-4 w-4 text-muted-foreground" />
                <code className="font-mono text-primary font-medium">GET /healthz</code>
              </div>
              <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-slate-100 text-slate-800">健康检查</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
