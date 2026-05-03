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
      .catch(() => toast.error("Failed to refresh, please check session key"))
  }

  useEffect(() => {
    fetchKeys()
  }, [])

  const handleGenerate = () => {
    fetch(`${API_BASE}/api/admin/keys`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(async res => {
      const data = await res.json().catch(() => ({}))
      if (res.ok) {
        toast.success("New API Key generated")
        if (data.key) copyToClipboard(data.key)
        fetchKeys()
      } else {
        toast.error(data.detail || "Generation failed, please check permissions")
      }
    }).catch(() => toast.error("Generation failed, please check permissions"))
  }

  const handleDelete = (key: string) => {
    fetch(`${API_BASE}/api/admin/keys/${encodeURIComponent(key)}`, {
      method: "DELETE",
      headers: getAuthHeader()
    }).then(async res => {
      if (res.ok) {
        toast.success("API Key deleted")
        fetchKeys()
      } else {
        const data = await res.json().catch(() => ({}))
        toast.error(data.detail || "Deletion failed")
      }
    }).catch(() => toast.error("Deletion failed"))
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
          <h2 className="text-2xl font-bold tracking-tight">API Key Distribution</h2>
          <p className="text-muted-foreground">Manage downstream credentials that can access this gateway.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => { fetchKeys(); toast.success("Refreshed"); }}>
            <RefreshCw className="mr-2 h-4 w-4" /> Refresh
          </Button>
          <Button onClick={handleGenerate}>
            <Plus className="mr-2 h-4 w-4" /> Generate New Key
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
