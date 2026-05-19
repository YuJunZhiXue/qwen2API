import { useState, useEffect } from "react"
import { Settings2, RefreshCw, KeyRound, ServerCrash, Code } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { useTranslation } from "react-i18next"

export default function SettingsPage() {
  const { t } = useTranslation()
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
      .catch(() => toast.error(t("settings.fetchFailed")))
  }

  useEffect(() => {
    loadSessionKey()
    fetchSettings()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSaveSessionKey = () => {
    if (!sessionKey.trim()) {
      toast.error(t("settings.sessionKey.keyRequired"))
      return
    }
    localStorage.setItem('qwen2api_key', sessionKey.trim())
    toast.success(t("settings.sessionKey.saved"))
    fetchSettings()
  }

  const handleClearSessionKey = () => {
    localStorage.removeItem('qwen2api_key')
    setSessionKey("")
    toast.success(t("settings.sessionKey.cleared"))
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
      if(res.ok) { toast.success(t("settings.core.saveConcurrencyToast")); fetchSettings(); }
      else toast.error(t("settings.saveFailed"))
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
      if(res.ok) { toast.success(t("settings.pool.savePoolToast")); fetchSettings(); }
      else toast.error(t("settings.saveFailed"))
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
        if(res.ok) { toast.success(t("settings.modelAliases.saveAliasesToast")); fetchSettings(); }
        else toast.error(t("settings.saveFailed"))
      })
    } catch(_e) {
      toast.error(t("settings.modelAliases.jsonError"))
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
          <h2 className="text-2xl font-bold tracking-tight">{t("settings.title")}</h2>
          <p className="text-muted-foreground">{t("settings.subtitle")}</p>
        </div>
        <Button variant="outline" onClick={() => {fetchSettings(); toast.success(t("settings.configRefreshed"))}}>
          <RefreshCw className="mr-2 h-4 w-4" /> {t("settings.refreshConfig")}
        </Button>
      </div>

      <div className="grid gap-6 min-w-0">
        {/* Session Key */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <KeyRound className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">{t("settings.sessionKey.title")}</h3>
            </div>
            <p className="text-sm text-muted-foreground">{t("settings.sessionKey.subtitle")}</p>
          </div>
          <div className="p-6">
            <div className="flex gap-2 items-center flex-wrap">
              <input
                type="password"
                value={sessionKey}
                onChange={e => setSessionKey(e.target.value)}
                placeholder={t("settings.sessionKey.placeholder")}
                className="flex h-10 flex-1 min-w-[200px] rounded-md border border-input bg-background px-3 py-2 text-sm"
              />
              <Button onClick={handleSaveSessionKey}>{t("common.save")}</Button>
              <Button variant="ghost" onClick={handleClearSessionKey}>{t("common.clear")}</Button>
            </div>
          </div>
        </div>

        {/* Connection Info */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <ServerCrash className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">{t("settings.connection.title")}</h3>
            </div>
          </div>
          <div className="p-6">
            <div className="space-y-1 min-w-0">
              <label className="text-sm font-medium">{t("settings.connection.baseUrlLabel")}</label>
              <input type="text" readOnly value={baseUrl} className="flex h-10 w-full rounded-md border border-input bg-muted px-3 py-2 text-sm font-mono text-muted-foreground" />
            </div>
          </div>
        </div>

        {/* Core Settings */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">{t("settings.core.title")}</h3>
            </div>
            <p className="text-sm text-muted-foreground">{t("settings.core.subtitle")}</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-2">
              <div className="space-y-1 min-w-0">
                <span className="text-sm font-medium">{t("settings.core.version")}</span>
              </div>
              <span className="font-mono text-sm">{settings?.version || "..."}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">{t("settings.core.maxInflightLabel")}</span>
                <p className="text-xs text-muted-foreground">{t("settings.core.maxInflightDesc")}</p>
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
                <span className="text-sm font-medium">{t("settings.core.globalMaxInflightLabel")}</span>
                <p className="text-xs text-muted-foreground">{t("settings.core.globalMaxInflightDesc")}</p>
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
              <Button size="sm" onClick={handleSaveConcurrency}>{t("settings.core.saveConcurrency")}</Button>
            </div>
          </div>
        </div>

        {/* Chat ID Pool */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Settings2 className="h-5 w-5 text-rose-500" />
              <h3 className="font-semibold leading-none tracking-tight">{t("settings.pool.title")}</h3>
            </div>
            <p className="text-sm text-muted-foreground">{t("settings.pool.subtitle")}</p>
          </div>
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center py-2 border-b flex-wrap gap-4">
              <div className="space-y-1 min-w-0 flex-1">
                <span className="text-sm font-medium">{t("settings.pool.targetLabel")}</span>
                <p className="text-xs text-muted-foreground">{t("settings.pool.targetDesc")}</p>
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
                <span className="text-sm font-medium">{t("settings.pool.ttlLabel")}</span>
                <p className="text-xs text-muted-foreground">{t("settings.pool.ttlDesc")}</p>
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
              <Button size="sm" onClick={handleSavePool}>{t("settings.pool.savePool")}</Button>
            </div>
          </div>
        </div>

        {/* Model Mapping */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <h3 className="font-semibold leading-none tracking-tight">{t("settings.modelAliases.title")}</h3>
            <p className="text-sm text-muted-foreground">{t("settings.modelAliases.subtitle")}</p>
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
              <Button onClick={handleSaveAliases}>{t("settings.modelAliases.saveAliases")}</Button>
            </div>
          </div>
        </div>

        {/* Usage Example */}
        <div className="rounded-xl border bg-card text-card-foreground shadow-sm min-w-0">
          <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30">
            <div className="flex items-center gap-2">
              <Code className="h-5 w-5 text-primary" />
              <h3 className="font-semibold leading-none tracking-tight">{t("settings.example.title")}</h3>
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
