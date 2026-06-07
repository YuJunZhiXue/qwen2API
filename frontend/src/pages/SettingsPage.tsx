import { useCallback, useEffect, useState } from "react"
import { Settings2, RefreshCw, KeyRound, ServerCrash, Code, Activity, Save } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import {
  capabilityBadges,
  fetchModelOptions,
  formatModeLabel,
  formatModelName,
  groupModelOptions,
  type ModelOption,
} from "../lib/models"

type ModelAliases = Record<string, string>

interface AdminSettings {
  version?: string
  max_inflight_per_account?: number
  global_max_inflight?: number
  chat_id_pool_target?: number
  chat_id_pool_ttl_seconds?: number
  keepalive_url?: string
  keepalive_interval?: number
  keepalive_env_locked?: string[]
  keepalive_running?: boolean
  model_aliases?: ModelAliases
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<AdminSettings | null>(null)
  const [sessionKey, setSessionKey] = useState(() => localStorage.getItem("qwen2api_key") || "")
  const [maxInflight, setMaxInflight] = useState(4)
  const [globalMaxInflight, setGlobalMaxInflight] = useState(0)
  const [poolTarget, setPoolTarget] = useState(5)
  const [poolTtlMin, setPoolTtlMin] = useState(10)
  const [keepaliveUrl, setKeepaliveUrl] = useState("")
  const [keepaliveInterval, setKeepaliveInterval] = useState(60)
  const [keepaliveEnvLocked, setKeepaliveEnvLocked] = useState<string[]>([])
  const [keepaliveRunning, setKeepaliveRunning] = useState(false)
  const [modelAliases, setModelAliases] = useState("")
  const [models, setModels] = useState<ModelOption[]>([])
  const [modelsLoading, setModelsLoading] = useState(false)

  const fetchSettings = useCallback(() => {
    fetch(`${API_BASE}/api/admin/settings`, { headers: getAuthHeader() })
      .then(res => {
        if(!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => {
        setSettings(data)
        setMaxInflight(data.max_inflight_per_account || 4)
        setGlobalMaxInflight(data.global_max_inflight || 0)
        setPoolTarget(data.chat_id_pool_target || 5)
        setPoolTtlMin(Math.round((data.chat_id_pool_ttl_seconds || 600) / 60))
        setKeepaliveUrl(data.keepalive_url || "")
        setKeepaliveInterval(data.keepalive_interval || 60)
        setKeepaliveEnvLocked(data.keepalive_env_locked || [])
        setKeepaliveRunning(Boolean(data.keepalive_running))
        setModelAliases(JSON.stringify(data.model_aliases || {}, null, 2))
      })
      .catch(() => toast.error("配置获取失败，请检查会话 Key"))
  }, [])

  const fetchModels = useCallback(() => {
    setModelsLoading(true)
    fetchModelOptions()
      .then(setModels)
      .catch(() => setModels([]))
      .finally(() => setModelsLoading(false))
  }, [])

  useEffect(() => {
    queueMicrotask(() => {
      fetchSettings()
      fetchModels()
    })
  }, [fetchSettings, fetchModels])

  const handleSaveSessionKey = () => {
    if (!sessionKey.trim()) {
      toast.error("请输入 Key")
      return
    }
    localStorage.setItem('qwen2api_key', sessionKey.trim())
    toast.success("Key 已保存到本地，刷新数据...")
    fetchSettings()
  }

  const handleClearSessionKey = () => {
    localStorage.removeItem('qwen2api_key')
    setSessionKey("")
    toast.success("Key 已清除")
  }

  const handleSaveConcurrency = () => {
    fetch(`${API_BASE}/api/admin/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        max_inflight_per_account: Number(maxInflight),
        global_max_inflight: Number(globalMaxInflight),
      })
    }).then(res => {
      if(res.ok) { toast.success("并发配置已保存（运行时立即生效）"); fetchSettings(); }
      else toast.error("保存失败")
    })
  }

  const handleSavePool = () => {
    fetch(`${API_BASE}/api/admin/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        chat_id_pool_target: Number(poolTarget),
        chat_id_pool_ttl_seconds: Number(poolTtlMin) * 60,
      })
    }).then(res => {
      if(res.ok) { toast.success("预热池配置已保存（下一轮刷新生效）"); fetchSettings(); }
      else toast.error("保存失败")
    })
  }

  const handleUseCurrentKeepaliveUrl = () => {
    setKeepaliveUrl(`${baseUrl.replace(/\/$/, "")}/keepalive`)
  }

  const handleSaveKeepalive = () => {
    const interval = Number(keepaliveInterval)
    if (!Number.isFinite(interval) || interval < 5 || interval > 86400) {
      toast.error("保活间隔必须在 5 - 86400 秒之间")
      return
    }

    fetch(`${API_BASE}/api/admin/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        keepalive_url: keepaliveUrl.trim(),
        keepalive_interval: interval,
      })
    }).then(async res => {
      const data = await res.json().catch(() => ({}))
      if(res.ok) { toast.success("保活配置已保存（运行时立即生效）"); fetchSettings(); }
      else toast.error(data.detail || "保存失败")
    }).catch(() => toast.error("保存失败"))
  }

  const handleSaveAliases = () => {
    try {
      const parsed = JSON.parse(modelAliases)
      fetch(`${API_BASE}/api/admin/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify({ model_aliases: parsed })
      }).then(res => {
        if(res.ok) { toast.success("模型映射规则已更新"); fetchSettings(); }
        else toast.error("保存失败")
      })
    } catch {
      toast.error("JSON 格式错误，请检查语法")
    }
  }

  const baseUrl = API_BASE || `http://${window.location.hostname}:7860`
  const modelGroups = groupModelOptions(models)

  const curlExample = `# OpenAI streaming chat
  curl ${baseUrl}/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "model": "qwen3.6-plus",
      "messages": [{"role": "user", "content": "Hello"}],
      "stream": true
    }'

  # Upload one file first (the response contains a reusable content_block)
  curl ${baseUrl}/v1/files \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -F "file=@./context.txt"

  # OpenAI + attachment
  curl ${baseUrl}/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "model": "qwen3.6-plus",
      "stream": false,
      "messages": [
        {
          "role": "user",
          "content": [
            {"type": "text", "text": "Read the uploaded file and summarize the key points."},
            {"type": "input_file", "file_id": "FILE_ID_FROM_UPLOAD", "filename": "context.txt", "mime_type": "text/plain"}
          ]
        }
      ]
    }'

  # Anthropic / Claude Code + attachment
  curl ${baseUrl}/anthropic/v1/messages \
    -H "Content-Type: application/json" \
    -H "x-api-key: YOUR_API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -d '{
      "model": "claude-sonnet-4-6",
      "max_tokens": 1024,
      "messages": [
        {
          "role": "user",
          "content": [
            {"type": "text", "text": "Read the uploaded file and summarize the key points."},
            {"type": "input_file", "file_id": "FILE_ID_FROM_UPLOAD", "filename": "context.txt", "mime_type": "text/plain"}
          ]
        }
      ]
    }'

  # Gemini
  curl ${baseUrl}/v1beta/models/qwen3.6-plus:generateContent \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "contents": [{"parts": [{"text": "Hello"}]}]
    }'

  # Images
  curl ${baseUrl}/v1/images/generations \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "model": "qwen3.6-plus-image",
      "prompt": "A cyberpunk cat with neon lights, ultra realistic",
      "n": 1,
      "size": "1328x1328",
      "response_format": "url"
    }'

  # Video
  curl ${baseUrl}/v1/videos/generations \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "model": "qwen3.6-plus-video",
      "prompt": "Generate a slow-motion ocean-wave video.",
      "duration": 5,
      "size": "1664x928",
      "ratio": "16:9",
      "response_format": "url"
    }'`

  return (
    <div className="w-full max-w-5xl mx-auto min-w-0 overflow-x-hidden space-y-6">
      <div className="flex justify-between items-center flex-wrap gap-4">
        <div className="min-w-0">
          <h2 className="text-2xl font-bold tracking-tight">系统设置</h2>
          <p className="text-muted-foreground">管理控制台认证与网关运行时配置。</p>
        </div>
        <Button variant="outline" onClick={() => {fetchSettings(); fetchModels(); toast.success("配置已刷新")}}>
          <RefreshCw className="mr-2 h-4 w-4" /> 刷新配置
        </Button>
      </div>

      <div className="grid gap-6 min-w-0">
        {/* Session Key */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">当前会话 Key</h3>
            </div>
            <p className="text-sm text-muted-foreground">将已有的 API Key 粘贴到此处，控制台将使用它进行所有的管理操作。（保存在浏览器本地）</p>
          </div>
          <div className="p-6">
            <div className="flex gap-2 items-center flex-wrap">
              <input
                type="password"
                value={sessionKey}
                onChange={e => setSessionKey(e.target.value)}
                placeholder="sk-qwen-... 或默认管理员密钥 admin"
                className="flex h-10 flex-1 min-w-[200px] rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
              <Button onClick={handleSaveSessionKey}>保存</Button>
              <Button variant="ghost" onClick={handleClearSessionKey}>清除</Button>
            </div>
          </div>
        </div>

        {/* Connection Info */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <ServerCrash className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">连接信息</h3>
            </div>
          </div>
          <div className="p-6">
            <div className="space-y-1 min-w-0">
              <label className="text-sm font-medium">API 基础地址 (Base URL)</label>
              <input type="text" readOnly value={baseUrl} className="flex h-10 w-full rounded-md border border-input bg-muted px-3 py-2 text-sm font-mono text-muted-foreground" />
            </div>
          </div>
        </div>

        {/* Model Catalog */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">模型名称 / 模型目录</h3>
            </div>
            <p className="text-sm text-muted-foreground">从 /v1/models 读取当前可用模型，按系列折叠展示。同系列例如 qwen3.6 会归在一个分组里。</p>
          </div>
          <div className="p-6 space-y-3">
            {modelsLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <RefreshCw className="h-4 w-4 animate-spin" /> 正在读取模型列表...
              </div>
            ) : modelGroups.length === 0 ? (
              <div className="rounded-lg border border-dashed bg-muted/20 p-4 text-sm text-muted-foreground">
                暂无模型数据。请确认会话 Key 有权限访问 /v1/models。
              </div>
            ) : (
              modelGroups.map((group, index) => (
                <details key={group.family} open={index === 0} className="rounded-lg border bg-background/60">
                  <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold">
                    {group.family}
                    <span className="ml-2 rounded-full bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground">
                      {group.models.length} 个模型
                    </span>
                  </summary>
                  <div className="border-t divide-y">
                    {group.models.map(option => {
                      const badges = capabilityBadges(option)
                      return (
                        <div key={option.id} className="grid gap-2 px-4 py-3 text-sm md:grid-cols-[1.4fr_1fr_0.7fr_1fr] md:items-center">
                          <div className="min-w-0">
                            <div className="truncate font-medium">{formatModelName(option)}</div>
                            <div className="truncate font-mono text-xs text-muted-foreground">{option.id}</div>
                          </div>
                          <div className="min-w-0 font-mono text-xs text-muted-foreground">
                            base: {option.base_model || option.id}
                          </div>
                          <div>
                            <span className="rounded-full border bg-muted/50 px-2 py-0.5 text-xs">
                              {formatModeLabel(option.mode)}
                            </span>
                          </div>
                          <div className="flex flex-wrap gap-1">
                            {badges.length ? badges.map(label => (
                              <span key={label} className="rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs text-primary">
                                {label}
                              </span>
                            )) : (
                              <span className="text-xs text-muted-foreground">对话</span>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </details>
              ))
            )}
          </div>
        </div>

        {/* Core Settings */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">核心并发参数</h3>
            </div>
            <p className="text-sm text-muted-foreground">运行时并发槽位与排队阈值（需要在后端 config.json 中修改后重启生效）。</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-2">
              <div className="space-y-1 min-w-0">
                <span className="text-sm font-medium">当前系统版本</span>
              </div>
              <span className="font-mono text-sm">{settings?.version || "..."}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">单账号最大并发 (max_inflight_per_account)</span>
                <p className="text-xs text-muted-foreground">每个上游账号同时处理的请求数。太大易被封，太小不充分利用。</p>
              </div>
              <input
                type="number"
                min="1"
                max="10"
                value={maxInflight}
                onChange={e => setMaxInflight(Number(e.target.value))}
                className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center"
              />
            </div>
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">全局并发上限 (global_max_inflight)</span>
                <p className="text-xs text-muted-foreground">所有账号合计同时在途请求的硬上限。0 = 不限。对应 Dashboard 的"异步任务"峰值。</p>
              </div>
              <input
                type="number"
                min="0"
                max="200"
                value={globalMaxInflight}
                onChange={e => setGlobalMaxInflight(Number(e.target.value))}
                className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center"
              />
            </div>
            <div className="flex justify-end">
              <Button size="sm" onClick={handleSaveConcurrency}>保存并发设置</Button>
            </div>
          </div>
        </div>

        {/* Chat ID Pool */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-rose-500" />
              <h3 className="font-semibold leading-none tracking-tight">Chat_ID 预热池</h3>
            </div>
            <p className="text-sm text-muted-foreground">预建 chat_id 规避上游 /chats/new 握手 (0.5~6s)。运行时修改立即生效。</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">每账号目标数 (target)</span>
                <p className="text-xs text-muted-foreground">每个账号预先挂多少个 chat_id 等着。默认 5。</p>
              </div>
              <input
                type="number"
                min="0"
                max="20"
                value={poolTarget}
                onChange={e => setPoolTarget(Number(e.target.value))}
                className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center"
              />
            </div>
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">TTL (分钟)</span>
                <p className="text-xs text-muted-foreground">chat_id 超过此时长则丢弃重建，避免被上游静默回收。默认 10。</p>
              </div>
              <input
                type="number"
                min="1"
                max="120"
                value={poolTtlMin}
                onChange={e => setPoolTtlMin(Number(e.target.value))}
                className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center"
              />
            </div>
            <div className="flex justify-end">
              <Button size="sm" onClick={handleSavePool}>保存预热池设置</Button>
            </div>
          </div>
        </div>

        {/* Keepalive */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-emerald-500" />
              <h3 className="font-semibold leading-none tracking-tight">保活配置</h3>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${keepaliveRunning ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300" : "bg-muted text-muted-foreground"}`}>
                {keepaliveRunning ? "运行中" : "未启用"}
              </span>
            </div>
            <p className="text-sm text-muted-foreground">配置后服务会定期向该 URL 发送 GET 请求以保持在线；留空则禁用保活。</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <label className="text-sm font-medium">保活 URL</label>
                <Button variant="outline" size="sm" onClick={handleUseCurrentKeepaliveUrl} disabled={keepaliveEnvLocked.includes("keepalive_url")}>
                  <Activity className="mr-2 h-4 w-4" /> 一键设置保活
                </Button>
              </div>
              <input
                type="text"
                value={keepaliveUrl}
                disabled={keepaliveEnvLocked.includes("keepalive_url")}
                onChange={e => setKeepaliveUrl(e.target.value)}
                placeholder={`${baseUrl.replace(/\/$/, "")}/keepalive`}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm disabled:cursor-not-allowed disabled:bg-muted"
              />
              {keepaliveEnvLocked.includes("keepalive_url") && (
                <p className="text-xs text-muted-foreground">KEEPALIVE_URL 已由环境变量注入，面板不覆盖。</p>
              )}
            </div>
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">保活间隔（秒）</span>
                <p className="text-xs text-muted-foreground">范围 5 - 86400 秒，默认 60。</p>
              </div>
              <input
                type="number"
                min="5"
                max="86400"
                value={keepaliveInterval}
                disabled={keepaliveEnvLocked.includes("keepalive_interval")}
                onChange={e => setKeepaliveInterval(Number(e.target.value))}
                className="flex h-8 w-28 rounded-md border border-input bg-background px-3 py-1 text-sm text-center disabled:cursor-not-allowed disabled:bg-muted"
              />
            </div>
            {keepaliveEnvLocked.includes("keepalive_interval") && (
              <p className="text-xs text-muted-foreground">KEEPALIVE_INTERVAL 已由环境变量注入，面板不覆盖。</p>
            )}
            <div className="flex justify-end">
              <Button size="sm" onClick={handleSaveKeepalive}>
                <Save className="mr-2 h-4 w-4" /> 保存保活配置
              </Button>
            </div>
          </div>
        </div>

        {/* Model Mapping */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <h3 className="font-semibold leading-none tracking-tight">自动模型映射规则 (Model Aliases)</h3>
            <p className="text-sm text-muted-foreground">下游传入的模型名称将被网关自动路由至以下千问实际模型。请使用标准 JSON 格式编辑。</p>
          </div>
          <div className="p-6">
            <textarea
              rows={8}
              value={modelAliases}
              onChange={e => setModelAliases(e.target.value)}
              className="flex min-h-[160px] w-full rounded-md border border-input bg-slate-950 text-slate-300 px-3 py-2 text-sm font-mono"
              style={{ whiteSpace: "pre", overflowX: "auto" }}
            />
            <div className="mt-4 flex justify-end">
              <Button onClick={handleSaveAliases}>保存映射</Button>
            </div>
          </div>
        </div>

        {/* Usage Example */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Code className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">使用示例</h3>
            </div>
          </div>
          <div className="p-6 min-w-0">
            <pre className="bg-slate-950 rounded-lg p-4 text-xs font-mono text-slate-300 whitespace-pre-wrap break-all max-h-[400px] overflow-y-auto overflow-x-hidden">
              {curlExample}
            </pre>
          </div>
        </div>
      </div>
    </div>
  )
}
