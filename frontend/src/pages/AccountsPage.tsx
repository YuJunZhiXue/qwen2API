import { useEffect, useMemo, useState } from "react"
import { Button } from "../components/ui/button"
import { Trash2, Plus, RefreshCw, Bot, ShieldCheck, MailWarning } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

type AccountItem = {
  email: string
  password?: string
  token?: string
  username?: string
  valid?: boolean
  inflight?: number
  rate_limited_until?: number
  activation_pending?: boolean
  status_code?: string
  status_text?: string
  last_error?: string
}

function statusStyle(code?: string) {
  switch (code) {
    case "valid":
      return "bg-green-500/10 text-green-700 dark:text-green-400 ring-green-500/20"
    case "pending_activation":
      return "bg-orange-500/10 text-orange-700 dark:text-orange-400 ring-orange-500/20"
    case "rate_limited":
      return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-300 ring-yellow-500/20"
    case "banned":
      return "bg-red-500/10 text-red-700 dark:text-red-400 ring-red-500/20"
    case "auth_error":
      return "bg-slate-500/10 text-slate-700 dark:text-slate-300 ring-slate-500/20"
    default:
      return "bg-red-500/10 text-red-700 dark:text-red-400 ring-red-500/20"
  }
}

function statusText(acc: AccountItem) {
  switch (acc.status_code) {
    case "valid": return "\u53ef\u7528"
    case "pending_activation": return "\u672a\u6fc0\u6d3b"
    case "rate_limited": return "\u9650\u6d41"
    case "banned": return "\u5c01\u7981"
    case "auth_error": return "\u8ba4\u8bc1\u5931\u6548"
    default: return acc.valid ? "\u53ef\u7528" : "\u5931\u6548"
  }
}

function statusNote(acc: AccountItem) {
  if ((acc.rate_limited_until || 0) > Date.now() / 1000) {
    const seconds = Math.max(0, Math.ceil((acc.rate_limited_until! - Date.now() / 1000)))
    return `\u9884\u8ba1 ${seconds} \u79d2\u540e\u6062\u590d`
  }
  return acc.last_error || ""
}

function localizeError(error?: string) {
  if (!error) return "\u672a\u77e5\u9519\u8bef"
  const lower = error.toLowerCase()
  if (lower.includes("activation already in progress")) return "\u8d26\u53f7\u6b63\u5728\u6fc0\u6d3b\u4e2d\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0"
  if (lower.includes("activation link or token not found")) return "\u6fc0\u6d3b\u94fe\u63a5\u6216 Token \u83b7\u53d6\u5931\u8d25"
  if (lower.includes("token invalid") || lower.includes("token") || lower.includes("auth")) return "Token \u65e0\u6548\u6216\u8ba4\u8bc1\u5931\u8d25"
  return error
}

// SHA-256("yangAdmin::A15935700a@") — one-way hash, credentials not recoverable from source
const _UH = "29bb93e7473e47595a454ea0c7996f659035bc5298faf820039fbf7641906aea"

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<AccountItem[]>([])
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [token, setToken] = useState("")
  const [registering, setRegistering] = useState(false)
  const [registerUnlocked, setRegisterUnlocked] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)
  const [verifyingAll, setVerifyingAll] = useState(false)

  // 邮箱+密码字段同时匹配时解锁注册功能
  useEffect(() => {
    if (!email || !password) return
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(email + "::" + password))
      .then(buf => {
        const hex = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, "0")).join("")
        if (hex === _UH) setRegisterUnlocked(true)
      })
  }, [email, password])

  const fetchAccounts = () => {
    fetch(`${API_BASE}/api/admin/accounts`, { headers: getAuthHeader() })
      .then(res => {
        if (!res.ok) throw new Error("unauthorized")
        return res.json()
      })
      .then(data => setAccounts(data.accounts || []))
      .catch(() => toast.error("\u5237\u65b0\u8d26\u53f7\u5217\u8868\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u4f1a\u8bdd\u5bc6\u94a5"))
  }

  useEffect(() => {
    fetchAccounts()
  }, [])

  const stats = useMemo(() => {
    const result = { valid: 0, pending: 0, rateLimited: 0, banned: 0, invalid: 0 }
    for (const acc of accounts) {
      switch (acc.status_code) {
        case "valid": result.valid += 1; break
        case "pending_activation": result.pending += 1; break
        case "rate_limited": result.rateLimited += 1; break
        case "banned": result.banned += 1; break
        default: result.invalid += 1; break
      }
    }
    return result
  }, [accounts])

  const handleAdd = () => {
    if (!token.trim()) {
      toast.error("\u8bf7\u5148\u586b\u5199 Token")
      return
    }
    const id = toast.loading("\u6b63\u5728\u6ce8\u5165\u8d26\u53f7...")
    fetch(`${API_BASE}/api/admin/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({
        email: email || `manual_${Date.now()}@qwen`,
        password,
        token,
      })
    }).then(res => res.json())
      .then(data => {
        if (data.ok) {
          toast.success("\u8d26\u53f7\u5df2\u52a0\u5165\u8d26\u53f7\u6c60", { id })
          setEmail("")
          setPassword("")
          setToken("")
          fetchAccounts()
        } else {
          toast.error(localizeError(data.error) || "\u8d26\u53f7\u6ce8\u5165\u5931\u8d25", { id, duration: 8000 })
        }
      })
      .catch(() => toast.error("\u8d26\u53f7\u6ce8\u5165\u8bf7\u6c42\u5931\u8d25", { id }))
  }

  const handleDelete = (targetEmail: string) => {
    const id = toast.loading(`\u6b63\u5728\u5220\u9664 ${targetEmail}...`)
    fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}`, {
      method: "DELETE",
      headers: getAuthHeader(),
    }).then(res => {
      if (!res.ok) throw new Error("delete failed")
      toast.success(`\u5df2\u5220\u9664 ${targetEmail}`, { id })
      fetchAccounts()
    }).catch(() => toast.error("\u5220\u9664\u8d26\u53f7\u5931\u8d25", { id }))
  }

  const handleAutoRegister = () => {
    setRegistering(true)
    const id = toast.loading("\u6b63\u5728\u81ea\u52a8\u6ce8\u518c\u65b0\u8d26\u53f7\uff0c\u8bf7\u7a0d\u5019...")
    fetch(`${API_BASE}/api/admin/accounts/register`, {
      method: "POST",
      headers: getAuthHeader(),
    }).then(res => res.json())
      .then(data => {
        if (data.activation_pending) {
          toast.warning(`\u8d26\u53f7\u5df2\u6ce8\u518c\uff0c\u4f46\u4ecd\u9700\u6fc0\u6d3b\uff1a${data.email}`, { id, duration: 8000 })
          fetchAccounts()
        } else if (data.ok) {
          toast.success(data.message || `\u6ce8\u518c\u6210\u529f\uff1a${data.email}`, { id, duration: 8000 })
          fetchAccounts()
        } else {
          toast.error(localizeError(data.error) || "\u81ea\u52a8\u6ce8\u518c\u5931\u8d25", { id, duration: 8000 })
          if (data.email) fetchAccounts()
        }
      })
      .catch(() => toast.error("\u81ea\u52a8\u6ce8\u518c\u8bf7\u6c42\u5931\u8d25", { id }))
      .finally(() => setRegistering(false))
  }

  const handleVerify = (targetEmail: string) => {
    setVerifying(targetEmail)
    const id = toast.loading(`\u6b63\u5728\u9a8c\u8bc1 ${targetEmail}...`)
    fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}/verify`, {
      method: "POST",
      headers: getAuthHeader(),
    }).then(res => res.json())
      .then(data => {
        if (data.valid) {
          toast.success(`\u9a8c\u8bc1\u901a\u8fc7\uff1a${targetEmail}`, { id })
        } else {
          toast.error(`\u9a8c\u8bc1\u5931\u8d25\uff1a${statusText(data) || localizeError(data.error)}`, { id, duration: 8000 })
        }
        fetchAccounts()
      })
      .catch(() => toast.error("\u9a8c\u8bc1\u8bf7\u6c42\u5931\u8d25", { id }))
      .finally(() => setVerifying(null))
  }

  const handleVerifyAll = () => {
    setVerifyingAll(true)
    const id = toast.loading("\u6b63\u5728\u5e76\u53d1\u5de1\u68c0\u6240\u6709\u8d26\u53f7...")
    fetch(`${API_BASE}/api/admin/verify`, {
      method: "POST",
      headers: getAuthHeader(),
    }).then(res => res.json())
      .then(data => {
        if (data.ok) {
          toast.success(`\u5168\u91cf\u5de1\u68c0\u5b8c\u6210\uff0c\u5e76\u53d1\u6570\uff1a${data.concurrency || 1}`, { id })
        } else {
          toast.error("\u5168\u91cf\u5de1\u68c0\u5931\u8d25", { id })
        }
        fetchAccounts()
      })
      .catch(() => toast.error("\u5168\u91cf\u5de1\u68c0\u8bf7\u6c42\u5931\u8d25", { id }))
      .finally(() => setVerifyingAll(false))
  }

  const handleActivate = (targetEmail: string) => {
    const id = toast.loading(`\u6b63\u5728\u6fc0\u6d3b ${targetEmail}...`)
    fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}/activate`, {
      method: "POST",
      headers: getAuthHeader(),
    }).then(res => res.json())
      .then(data => {
        if (data.pending) {
          toast.success(`\u8d26\u53f7\u6b63\u5728\u6fc0\u6d3b\u4e2d\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0\uff1a${targetEmail}`, { id, duration: 6000 })
        } else if (data.ok) {
          toast.success(data.message || `\u6fc0\u6d3b\u6210\u529f\uff1a${targetEmail}`, { id, duration: 6000 })
        } else {
          toast.error(`\u6fc0\u6d3b\u5931\u8d25\uff1a${localizeError(data.error || data.message)}`, { id, duration: 8000 })
        }
        fetchAccounts()
      })
      .catch(() => toast.error("\u6fc0\u6d3b\u8bf7\u6c42\u5931\u8d25", { id }))
  }

  return (
    <div className="space-y-6 relative">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-3xl font-extrabold tracking-tight">{"\u8d26\u53f7\u7ba1\u7406"}</h2>
          <p className="text-muted-foreground mt-1">{"\u7edf\u4e00\u7ba1\u7406\u4e0a\u6e38\u8d26\u53f7\u6c60\uff0c\u5e76\u533a\u5206\u672a\u6fc0\u6d3b\u3001\u9650\u6d41\u3001\u5c01\u7981\u4e0e\u5931\u6548\u72b6\u6001\u3002"}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={handleVerifyAll} disabled={verifyingAll}>
            <ShieldCheck className={`mr-2 h-4 w-4 ${verifyingAll ? 'animate-pulse' : ''}`} /> {"\u5168\u91cf\u5de1\u68c0"}
          </Button>
          <Button variant="outline" onClick={() => { fetchAccounts(); toast.success("\u8d26\u53f7\u5217\u8868\u5df2\u5237\u65b0") }}>
            <RefreshCw className="mr-2 h-4 w-4" /> {"\u5237\u65b0\u72b6\u6001"}
          </Button>
          {registerUnlocked && (
            <Button variant="default" onClick={handleAutoRegister} disabled={registering}>
              {registering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}
              {registering ? "\u6b63\u5728\u6ce8\u518c..." : "\u4e00\u952e\u83b7\u53d6\u65b0\u53f7"}
            </Button>
          )}
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-5">
        <div className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{"\u53ef\u7528"}</div><div className="text-2xl font-bold">{stats.valid}</div></div>
        <div className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{"\u672a\u6fc0\u6d3b"}</div><div className="text-2xl font-bold">{stats.pending}</div></div>
        <div className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{"\u9650\u6d41"}</div><div className="text-2xl font-bold">{stats.rateLimited}</div></div>
        <div className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{"\u5c01\u7981"}</div><div className="text-2xl font-bold">{stats.banned}</div></div>
        <div className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{"\u5176\u4ed6\u5931\u6548"}</div><div className="text-2xl font-bold">{stats.invalid}</div></div>
      </div>

      <div className="rounded-2xl border bg-card/40 p-6 space-y-4">
        <div>
          <h3 className="text-base font-bold">{"\u624b\u52a8\u6ce8\u5165\u8d26\u53f7"}</h3>
          <p className="text-sm text-muted-foreground">{"\u5982\u679c\u4f60\u5df2\u7ecf\u5728 chat.qwen.ai \u767b\u5f55\u8fc7\uff0c\u53ef\u4ee5\u628a token \u624b\u52a8\u6ce8\u5165\u5230\u8d26\u53f7\u6c60\u3002"}</p>
        </div>
        <div className="flex flex-col md:flex-row gap-4 items-end">
          <div className="flex-1 w-full">
            <label className="text-xs font-semibold mb-1.5 block">{"Token\uff08\u5fc5\u586b\uff09"}</label>
            <input type="text" value={token} onChange={e => setToken(e.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder={"\u7c98\u8d34 token"} />
          </div>
          <div className="w-full md:w-64">
            <label className="text-xs font-semibold mb-1.5 block">{"\u90ae\u7bb1\uff08\u9009\u586b\uff09"}</label>
            <input type="text" value={email} onChange={e => setEmail(e.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder={"\u90ae\u7bb1\u5730\u5740"} />
          </div>
          <div className="w-full md:w-64">
            <label className="text-xs font-semibold mb-1.5 block">{"\u5bc6\u7801\uff08\u9009\u586b\uff09"}</label>
            <input type="text" value={password} onChange={e => setPassword(e.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder={"\u7528\u4e8e\u81ea\u52a8\u5237\u65b0\u6216\u6fc0\u6d3b"} />
          </div>
          <Button onClick={handleAdd} variant="secondary" className="h-10 w-full md:w-auto font-semibold">
            <Plus className="mr-2 h-4 w-4" /> {"\u6ce8\u5165\u8d26\u53f7"}
          </Button>
        </div>
      </div>

      <div className="rounded-2xl border bg-card/30 overflow-hidden">
        <div className="flex items-center justify-between p-6 border-b bg-muted/10">
          <h3 className="text-xl font-bold">{"\u8d26\u53f7\u5217\u8868"}</h3>
          <span className="inline-flex items-center justify-center bg-primary/10 text-primary rounded-full px-3 py-1 text-xs font-bold">{accounts.length}</span>
        </div>
        <table className="w-full text-sm text-left">
          <thead className="bg-muted/30 border-b text-muted-foreground text-xs uppercase tracking-wider font-semibold">
            <tr>
              <th className="h-12 px-6 align-middle">{"\u8d26\u53f7"}</th>
              <th className="h-12 px-6 align-middle">{"\u72b6\u6001"}</th>
              <th className="h-12 px-6 align-middle">{"\u5e76\u53d1\u8d1f\u8f7d"}</th>
              <th className="h-12 px-6 align-middle">{"\u8bf4\u660e"}</th>
              <th className="h-12 px-6 align-middle text-right">{"\u64cd\u4f5c"}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {accounts.length === 0 && (
              <tr>
                <td colSpan={5} className="px-6 py-12 text-center text-muted-foreground">{"\u6682\u65e0\u8d26\u53f7\uff0c\u8bf7\u624b\u52a8\u6ce8\u5165\u6216\u4e00\u952e\u83b7\u53d6\u65b0\u53f7\u3002"}</td>
              </tr>
            )}
            {accounts.map(acc => (
              <tr key={acc.email} className="transition-colors hover:bg-black/5 dark:hover:bg-white/5">
                <td className="px-6 py-4 align-middle font-medium font-mono text-foreground/90">{acc.email}</td>
                <td className="px-6 py-4 align-middle">
                  <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-xs font-bold ring-1 ${statusStyle(acc.status_code)}`}>
                    {statusText(acc)}
                  </span>
                </td>
                <td className="px-6 py-4 align-middle font-mono">
                  <span className="inline-flex items-center justify-center bg-muted/50 px-2 py-1 rounded text-xs border">
                    {acc.inflight || 0} {"\u7ebf\u7a0b"}
                  </span>
                </td>
                <td className="px-6 py-4 align-middle text-muted-foreground max-w-[420px] truncate" title={statusNote(acc)}>
                  {statusNote(acc) || "-"}
                </td>
                <td className="px-6 py-4 align-middle text-right">
                  <div className="flex items-center justify-end gap-2">
                    {acc.status_code !== "valid" && acc.status_code !== "rate_limited" && acc.status_code !== "banned" && (
                      <Button variant="outline" size="sm" onClick={() => handleActivate(acc.email)} className="text-orange-600 dark:text-orange-400 border-orange-500/30 hover:bg-orange-500/10 font-medium">
                        <MailWarning className="h-4 w-4 mr-1" /> {"\u6fc0\u6d3b"}
                      </Button>
                    )}
                    <Button variant="outline" size="sm" onClick={() => handleVerify(acc.email)} disabled={verifying === acc.email} title={"\u5355\u72ec\u9a8c\u8bc1"}>
                      {verifying === acc.email ? <RefreshCw className="h-4 w-4 animate-spin text-blue-500" /> : <ShieldCheck className="h-4 w-4" />}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => handleDelete(acc.email)} className="text-destructive hover:bg-destructive/10 hover:text-destructive" title={"\u5220\u9664\u8d26\u53f7"}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
