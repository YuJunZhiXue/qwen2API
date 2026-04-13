import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Plus, RefreshCw, Copy, Check, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

export default function TokensPage() {
  const [keys, setKeys] = useState<string[]>([])
  const [copied, setCopied] = useState<string | null>(null)

  const fetchKeys = () => {
    fetch(`${API_BASE}/api/admin/keys`, { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => setKeys(data.keys || []))
      .catch(() => toast.error("刷新失败，请检查会话 Key"))
  }

  useEffect(() => {
    fetchKeys()
  }, [])

  const handleGenerate = () => {
    fetch(`${API_BASE}/api/admin/keys`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => {
      if (res.ok) {
        toast.success("已生成新的 API Key")
        fetchKeys()
      } else {
        toast.error("生成失败，请检查权限")
      }
    })
  }

  const handleDelete = (key: string) => {
    fetch(`${API_BASE}/api/admin/keys/${encodeURIComponent(key)}`, {
      method: "DELETE",
      headers: getAuthHeader()
    }).then(res => {
      if (res.ok) {
        toast.success("API Key 已删除")
        fetchKeys()
      } else {
        toast.error("删除失败")
      }
    })
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
          <Button onClick={handleGenerate}>
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
            {keys.map((k, i) => (
              <tr key={k} className="border-b transition-colors hover:bg-muted/50">
                <td className="p-4 align-middle font-medium text-muted-foreground">{i + 1}</td>
                <td className="p-4 align-middle font-mono text-xs">{k}</td>
                <td className="p-4 align-middle text-right space-x-2">
                  <Button variant="ghost" size="sm" onClick={() => copyToClipboard(k)}>
                    {copied === k ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => handleDelete(k)} className="text-destructive hover:bg-destructive/10 hover:text-destructive">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
