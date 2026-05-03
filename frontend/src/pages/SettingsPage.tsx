import { useState, useEffect } from "react"
import { Settings2, RefreshCw, KeyRound, ServerCrash, Code } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

export default function SettingsPage() {
  const [settings, setSettings] = useState<any>(null)
  const [sessionKey, setSessionKey] = useState("")
  const [maxInflight, setMaxInflight] = useState(4)
  const [globalMaxInflight, setGlobalMaxInflight] = useState(0)
  const [poolTarget, setPoolTarget] = useState(5)
  const [poolTtlMin, setPoolTtlMin] = useState(10)
  const [modelAliases, setModelAliases] = useState("")

  const loadSessionKey = () => {
    setSessionKey(localStorage.getItem('qwen2api_key') || "")
  }

  const fetchSettings = () => {
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
        setModelAliases(JSON.stringify(data.model_aliases || {}, null, 2))
      })
      .catch(() => toast.error("Failed to fetch settings, please check session key"))
  }

  useEffect(() => {
    loadSessionKey()
    fetchSettings()
  }, [])

  const handleSaveSessionKey = () => {
    if (!sessionKey.trim()) {
      toast.error("Please enter Key")
      return
    }
    localStorage.setItem('qwen2api_key', sessionKey.trim())
    toast.success("Key saved locally, refreshing data...")
    fetchSettings()
  }

  const handleClearSessionKey = () => {
    localStorage.removeItem('qwen2api_key')
    setSessionKey("")
    toast.success("Key cleared")
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
      if(res.ok) { toast.success("Concurrency settings saved (takes effect immediately)"); fetchSettings(); }
      else toast.error("Save failed")
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
      if(res.ok) { toast.success("Warmup pool settings saved (takes effect on next refresh)"); fetchSettings(); }
      else toast.error("Save failed")
    })
  }

  const handleSaveAliases = () => {
    try {
      const parsed = JSON.parse(modelAliases)
      fetch(`${API_BASE}/api/admin/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify({ model_aliases: parsed })
      }).then(res => {
        if(res.ok) { toast.success("Model mapping rules updated"); fetchSettings(); }
        else toast.error("Save failed")
      })
    } catch(e) {
      toast.error("JSON format error, please check syntax")
    }
  }

  const baseUrl = API_BASE || `http://${window.location.hostname}:7860`

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
      "model": "dall-e-3",
      "prompt": "A cyberpunk cat with neon lights, ultra realistic",
      "n": 1,
      "size": "1024x1024",
      "response_format": "url"
    }'

  # Video (reserved path)
  curl ${baseUrl}/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{
      "model": "qwen3.6-plus",
      "stream": false,
      "messages": [{"role": "user", "content": "Generate a slow-motion ocean-wave video."}]
    }'`

  return (
    <div className="w-full max-w-5xl mx-auto min-w-0 overflow-x-hidden space-y-6">
      <div className="flex justify-between items-center flex-wrap gap-4">
        <div className="min-w-0">
          <h2 className="text-2xl font-bold tracking-tight">系统设置</h2>
          <p className="text-muted-foreground">管理控制台认证与网关运行时配置。</p>
        </div>
        <Button variant="outline" onClick={() => {fetchSettings(); toast.success("配置已刷新")}}>
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
