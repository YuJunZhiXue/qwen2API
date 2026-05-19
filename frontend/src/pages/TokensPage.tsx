import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Plus, RefreshCw, Copy, Check, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { useTranslation } from "react-i18next"

export default function TokensPage() {
  const { t } = useTranslation()
  const [keys, setKeys] = useState<string[]>([])
  const [copied, setCopied] = useState<string | null>(null)

  const fetchKeys = () => {
    fetch(`${API_BASE}/api/admin/keys`, { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("Unauthorized")
        return res.json()
      })
      .then(data => setKeys(data.keys || []))
      .catch(() => toast.error(t("tokens.fetchFailed")))
  }

  useEffect(() => {
    fetchKeys()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleGenerate = () => {
    fetch(`${API_BASE}/api/admin/keys`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(async res => {
      const data = await res.json().catch(() => ({}))
      if (res.ok) {
        toast.success(t("tokens.generated"))
        if (data.key) copyToClipboard(data.key)
        fetchKeys()
      } else {
        toast.error(data.detail || t("tokens.generateFailed"))
      }
    }).catch(() => toast.error(t("tokens.generateFailed")))
  }

  const handleDelete = (key: string) => {
    fetch(`${API_BASE}/api/admin/keys/${encodeURIComponent(key)}`, {
      method: "DELETE",
      headers: getAuthHeader()
    }).then(async res => {
      if (res.ok) {
        toast.success(t("tokens.deleted"))
        fetchKeys()
      } else {
        const data = await res.json().catch(() => ({}))
        toast.error(data.detail || t("tokens.deleteFailed"))
      }
    }).catch(() => toast.error(t("tokens.deleteFailed")))
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
          <h2 className="text-2xl font-bold tracking-tight">{t("tokens.title")}</h2>
          <p className="text-muted-foreground">{t("tokens.subtitle")}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => { fetchKeys(); toast.success(t("tokens.refreshed")); }}>
            <RefreshCw className="mr-2 h-4 w-4" /> {t("tokens.refresh")}
          </Button>
          <Button onClick={handleGenerate}>
            <Plus className="mr-2 h-4 w-4" /> {t("tokens.generate")}
          </Button>
        </div>
      </div>

      <div className="rounded-xl border bg-card overflow-hidden">
        <table className="w-full text-sm text-left">
          <thead className="bg-muted/50 border-b text-muted-foreground">
            <tr>
              <th className="h-12 px-4 align-middle font-medium w-16">{t("tokens.table.index")}</th>
              <th className="h-12 px-4 align-middle font-medium">API Key</th>
              <th className="h-12 px-4 align-middle font-medium text-right">{t("tokens.table.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 && (
              <tr>
                <td colSpan={3} className="p-4 text-center text-muted-foreground">{t("tokens.table.empty")}</td>
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
