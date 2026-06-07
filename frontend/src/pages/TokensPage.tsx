import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Plus, RefreshCw, Copy, Check, Trash2, X, KeyRound } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

type ApiKeyItem = {
  key: string
  source?: "env" | "managed"
  label?: string
}

export default function TokensPage() {
  const [keys, setKeys] = useState<ApiKeyItem[]>([])
  const [copied, setCopied] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createMode, setCreateMode] = useState<"auto" | "custom">("auto")
  const [customKey, setCustomKey] = useState("")

  const fetchKeys = () => {
    fetch(`${API_BASE}/api/admin/keys`, { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => {
        if (Array.isArray(data.items)) {
          setKeys(data.items)
        } else {
          setKeys((data.keys || []).map((key: string) => ({ key, source: "managed", label: "面板创建 Key" })))
        }
      })
      .catch(() => toast.error("刷新失败，请检查会话 Key"))
  }

  useEffect(() => {
    fetchKeys()
  }, [])

  const handleCreate = () => {
    if (createMode === "custom" && !customKey.trim()) {
      toast.error("请输入自定义 API Key")
      return
    }

    fetch(`${API_BASE}/api/admin/keys`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        mode: createMode,
        key: createMode === "custom" ? customKey.trim() : "",
      })
    }).then(async res => {
      const data = await res.json().catch(() => ({}))
      if (res.ok) {
        toast.success(createMode === "custom" ? "自定义 API Key 已添加" : "已生成新的 API Key")
        if (data.key) copyToClipboard(data.key)
        setCreateOpen(false)
        setCustomKey("")
        setCreateMode("auto")
        fetchKeys()
      } else {
        toast.error(data.detail || "创建失败，请检查权限")
      }
    }).catch(() => toast.error("创建失败，请检查权限"))
  }

  const handleDelete = (item: ApiKeyItem) => {
    if (item.source === "env") {
      toast.error("环境变量注入 Key 不能在面板删除")
      return
    }

    fetch(`${API_BASE}/api/admin/keys/${encodeURIComponent(item.key)}`, {
      method: "DELETE",
      headers: getAuthHeader()
    }).then(async res => {
      if (res.ok) {
        toast.success("API Key 已删除")
        fetchKeys()
      } else {
        const data = await res.json().catch(() => ({}))
        toast.error(data.detail || "删除失败")
      }
    }).catch(() => toast.error("删除失败"))
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(text)
    setTimeout(() => setCopied(null), 2000)
  }

  return (
    <div className="space-y-6 max-w-4xl">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">API Key 分发</h2>
          <p className="text-muted-foreground">管理可以访问此网关的下游凭证。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => { fetchKeys(); toast.success("已刷新"); }}>
            <RefreshCw className="mr-2 h-4 w-4" /> 刷新
          </Button>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="mr-2 h-4 w-4" /> 生成新 Key
          </Button>
        </div>
      </div>

      <div className="rounded-xl border bg-card overflow-hidden">
        <table className="w-full text-sm text-left">
          <thead className="bg-muted/50 border-b text-muted-foreground">
            <tr>
              <th className="h-12 px-4 align-middle font-medium w-16">序号</th>
              <th className="h-12 px-4 align-middle font-medium">API Key</th>
              <th className="h-12 px-4 align-middle font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 && (
              <tr>
                <td colSpan={3} className="p-4 text-center text-muted-foreground">暂无 API Key</td>
              </tr>
            )}
            {keys.map((item, i) => (
              <tr key={item.key} className="border-b transition-colors hover:bg-muted/50">
                <td className="p-4 align-middle font-medium text-muted-foreground">{i + 1}</td>
                <td className="p-4 align-middle">
                  <div className="flex flex-col gap-1 min-w-0">
                    <span className="font-mono text-xs break-all">{item.key}</span>
                    {item.source === "env" && (
                      <span className="w-fit rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                        {item.label || "环境变量注入 Key"}
                      </span>
                    )}
                  </div>
                </td>
                <td className="p-4 align-middle text-right space-x-2">
                  <Button variant="ghost" size="sm" onClick={() => copyToClipboard(item.key)} title="复制">
                    {copied === item.key ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(item)}
                    disabled={item.source === "env"}
                    title={item.source === "env" ? "环境变量 Key 需要从环境变量中移除" : "删除"}
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border bg-card shadow-xl">
            <div className="flex items-center justify-between border-b p-4">
              <div className="flex items-center gap-2">
                <KeyRound className="h-5 w-5 text-primary" />
                <h3 className="font-semibold">创建 API Key</h3>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setCreateOpen(false)} title="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="space-y-4 p-4">
              <div className="grid grid-cols-2 rounded-md border bg-muted/30 p-1">
                <button
                  type="button"
                  onClick={() => setCreateMode("auto")}
                  className={`rounded px-3 py-2 text-sm font-medium ${createMode === "auto" ? "bg-background shadow-sm" : "text-muted-foreground"}`}
                >
                  自动生成
                </button>
                <button
                  type="button"
                  onClick={() => setCreateMode("custom")}
                  className={`rounded px-3 py-2 text-sm font-medium ${createMode === "custom" ? "bg-background shadow-sm" : "text-muted-foreground"}`}
                >
                  自定义 Key
                </button>
              </div>

              {createMode === "custom" && (
                <div className="space-y-2">
                  <label className="text-sm font-medium">API Key</label>
                  <input
                    type="text"
                    value={customKey}
                    onChange={e => setCustomKey(e.target.value)}
                    placeholder="sk-your-custom-key"
                    className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm"
                  />
                </div>
              )}

              <div className="flex justify-end gap-2 pt-2">
                <Button variant="outline" onClick={() => setCreateOpen(false)}>取消</Button>
                <Button onClick={handleCreate}>
                  <Plus className="mr-2 h-4 w-4" /> 创建
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
