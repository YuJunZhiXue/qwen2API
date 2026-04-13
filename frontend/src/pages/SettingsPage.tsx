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
        setModelAliases(JSON.stringify(data.model_aliases || {}, null, 2))
      })
      .catch(() => toast.error("配置获取失败，请检查会话 Key"))
  }

  useEffect(() => {
    loadSessionKey()
    fetchSettings()
  }, [])

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
      body: JSON.stringify({ max_inflight_per_account: Number(maxInflight) })
    }).then(res => {
      if(res.ok) { toast.success("并发配置已保存"); fetchSettings(); }
      else toast.error("保存失败")
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
        if(res.ok) { toast.success("模型映射规则已更新"); fetchSettings(); }
        else toast.error("保存失败")
      })
    } catch(e) {
      toast.error("JSON 格式错误，请检查语法")
    }
  }

  const baseUrl = API_BASE || `http://${window.location.hostname}:7860`

  const curlExample = `# OpenAI 流式对话
curl ${baseUrl}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "qwen3.6-plus",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'

# Anthropic 格式（Claude Code / SDK）
curl ${baseUrl}/anthropic/v1/messages \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: YOUR_API_KEY" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'

# Gemini 格式
curl ${baseUrl}/v1beta/models/qwen3.6-plus:generateContent \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "contents": [{"parts": [{"text": "你好"}]}]
  }'

# 图片生成（标准 OpenAI Images 接口，推荐）
curl ${baseUrl}/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "dall-e-3",
    "prompt": "一只赛博朋克风格的猫，霓虹灯背景，超写实",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'

# 图片生成（Chat 意图识别自动路由，返回内容中附带图片链接）
curl ${baseUrl}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "qwen3.6-plus",
    "stream": false,
    "messages": [{"role": "user", "content": "帮我生成一张星空下的雪山图片，写实风格"}]
  }'

# 视频生成（仍为预留链路，先不要作为稳定能力依赖）
curl ${baseUrl}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "qwen3.6-plus",
    "stream": false,
    "messages": [{"role": "user", "content": "生成视频：海浪拍打礁石，慢动作"}]
  }'`

  return (
    <div className="space-y-6 max-w-4xl">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">系统设置</h2>
          <p className="text-muted-foreground">管理控制台认证与网关运行时配置。</p>
        </div>
        <Button variant="outline" onClick={() => {fetchSettings(); toast.success("配置已刷新")}}>
          <RefreshCw className="mr-2 h-4 w-4" /> 刷新配置
        </Button>
      </div>

      <div className="grid gap-6">
        {/* Session Key */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">当前会话 Key</h3>
            </div>
            <p className="text-sm text-muted-foreground">将已有的 API Key 粘贴到此处，控制台将使用它进行所有的管理操作。（保存在浏览器本地）</p>
          </div>
          <div className="p-6">
            <div className="flex gap-2 items-center">
              <input 
                type="password" 
                value={sessionKey}
                onChange={e => setSessionKey(e.target.value)}
                placeholder="sk-qwen-... 或默认管理员密钥 admin" 
                className="flex h-10 w-full flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
              <Button onClick={handleSaveSessionKey}>保存</Button>
              <Button variant="ghost" onClick={handleClearSessionKey}>清除</Button>
            </div>
          </div>
        </div>

        {/* Connection Info */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <ServerCrash className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">连接信息</h3>
            </div>
          </div>
          <div className="p-6">
            <div className="space-y-1">
              <label className="text-sm font-medium">API 基础地址 (Base URL)</label>
              <input type="text" readOnly value={baseUrl} className="flex h-10 w-full rounded-md border border-input bg-muted px-3 py-2 text-sm font-mono text-muted-foreground" />
            </div>
          </div>
        </div>

        {/* Core Settings */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">核心并发参数</h3>
            </div>
            <p className="text-sm text-muted-foreground">运行时并发槽位与排队阈值（需要在后端 config.json 中修改后重启生效）。</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center py-2 border-b">
              <div className="space-y-1">
                <span className="text-sm font-medium">当前系统版本</span>
              </div>
              <span className="font-mono text-sm">{settings?.version || "..."}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-b">
              <div className="space-y-1">
                <span className="text-sm font-medium">单账号最大并发 (max_inflight)</span>
                <p className="text-xs text-muted-foreground">控制每个上游账号同时处理的请求数量，避免被封禁。</p>
              </div>
              <div className="flex gap-2 items-center">
                <input 
                  type="number" 
                  min="1" 
                  max="10" 
                  value={maxInflight} 
                  onChange={e => setMaxInflight(Number(e.target.value))}
                  className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center"
                />
                <Button size="sm" onClick={handleSaveConcurrency}>保存</Button>
              </div>
            </div>
          </div>
        </div>

        {/* Model Mapping */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
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
            />
            <div className="mt-4 flex justify-end">
              <Button onClick={handleSaveAliases}>保存映射</Button>
            </div>
          </div>
        </div>

        {/* Usage Example */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Code className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">使用示例</h3>
            </div>
          </div>
          <div className="p-6">
            <div className="bg-slate-950 rounded-lg p-4 text-sm font-mono text-slate-300 overflow-x-auto whitespace-pre">
              {curlExample}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
