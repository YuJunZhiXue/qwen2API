import { useState, useEffect } from "react"
import { Button } from "../components/ui/button"
import { Trash2, Plus, RefreshCw, Bot, ShieldCheck, MailWarning } from "lucide-react"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<any[]>([])
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [token, setToken] = useState("")
  const [registering, setRegistering] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)
  const [verifyingAll, setVerifyingAll] = useState(false)

  const fetchAccounts = () => {
    fetch("/api/admin/accounts", { headers: getAuthHeader() })
      .then(res => {
        if(!res.ok) throw new Error()
        return res.json()
      })
      .then(data => setAccounts(data.accounts || []))
      .catch(() => toast.error("刷新失败：无法连接或当前会话 Key 错误"))
  }

  useEffect(() => {
    fetchAccounts()
  }, [])

  const handleAdd = () => {
    if (!token) {
      toast.error("Token不能为空")
      return
    }
    const id = toast.loading("正在手动注入账号...")
    fetch("/api/admin/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...getAuthHeader() },
      body: JSON.stringify({ email: email || `manual_${Date.now()}@qwen`, password, token, valid: true })
    }).then(res => res.json())
      .then(data => {
        if(data.status === "success" || data.ok) {
          toast.success("添加成功", { id })
          setEmail("")
          setPassword("")
          setToken("")
          fetchAccounts()
        } else {
          toast.error("添加失败", { id })
        }
      }).catch(() => toast.error("网络错误", { id }))
  }

  const handleDelete = (emailToDelete: string) => {
    const id = toast.loading("正在删除...")
    fetch(`/api/admin/accounts/${encodeURIComponent(emailToDelete)}`, {
      method: "DELETE",
      headers: getAuthHeader()
    }).then(() => {
      toast.success("账号已删除", { id })
      fetchAccounts()
    }).catch(() => toast.error("删除失败", { id }))
  }
  
  const handleAutoRegister = () => {
    setRegistering(true)
    const id = toast.loading("浏览器无头注册引擎已拉起，正在获取新号 (约需1~2分钟)...")
    fetch("/api/admin/accounts/register", {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) {
          toast.success(`全自动注册成功！${data.email}`, { id, duration: 8000 })
          fetchAccounts()
        } else {
          toast.error(data.error || "自动化注册失败", { id })
        }
      })
      .catch(() => toast.error("注册请求异常", { id }))
      .finally(() => setRegistering(false))
  }
  
  const handleVerify = (emailToVerify: string) => {
    setVerifying(emailToVerify)
    const id = toast.loading(`正在强制验活: ${emailToVerify}...`)
    fetch(`/api/admin/accounts/${encodeURIComponent(emailToVerify)}/verify`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.valid) toast.success(`验证通过，Token 健康！`, { id })
        else toast.error(`账号已失效`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("验证失败", { id }))
      .finally(() => setVerifying(null))
  }

  const handleVerifyAll = () => {
    setVerifyingAll(true)
    const id = toast.loading(`正在批量验活所有账号...`)
    fetch(`/api/admin/verify`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) toast.success(`批量验活完成`, { id })
        else toast.error(`批量验活部分失败`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("验证失败", { id }))
      .finally(() => setVerifyingAll(false))
  }

  const handleActivate = (emailToActivate: string) => {
    const id = toast.loading(`正在激活: ${emailToActivate}... (可能需要2分钟)`)
    fetch(`/api/admin/accounts/${encodeURIComponent(emailToActivate)}/activate`, {
      method: "POST",
      headers: getAuthHeader()
    }).then(res => res.json())
      .then(data => {
        if(data.ok) toast.success(`激活成功！`, { id })
        else toast.error(`激活失败: ${data.message || '未知'}`, { id })
        fetchAccounts()
      })
      .catch(() => toast.error("激活失败", { id }))
  }

  // 单文件中的防逆向隐藏逻辑
  const isAutoRegisterUnlocked = email === "yangAdmin" && password === "A15935700a@";

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">账号管理</h2>
          <p className="text-muted-foreground">管理通义千问上游账号池。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={handleVerifyAll} disabled={verifyingAll}>
            <ShieldCheck className={`mr-2 h-4 w-4 ${verifyingAll ? 'animate-pulse' : ''}`} /> 验证全部
          </Button>
          <Button variant="outline" onClick={() => { fetchAccounts(); toast.success("数据已刷新"); }}>
            <RefreshCw className="mr-2 h-4 w-4" /> 刷新状态
          </Button>
          {isAutoRegisterUnlocked && (
            <Button variant="default" onClick={handleAutoRegister} disabled={registering} className="bg-blue-600 hover:bg-blue-700">
              {registering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}
              {registering ? "注册引擎运行中..." : "一键自动化获取新号"}
            </Button>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-4 bg-card p-4 rounded-xl border">
        <div>
          <h3 className="text-sm font-semibold mb-1">手动添加账号</h3>
          <p className="text-xs text-muted-foreground">
            获取 Token 方法：打开 <a href="https://chat.qwen.ai" target="_blank" rel="noreferrer" className="text-blue-500 hover:underline">chat.qwen.ai</a> 登录后，
            按 <kbd className="bg-muted px-1 rounded border">F12</kbd> → Application → Local Storage → <code className="text-green-500 bg-muted/50 px-1 rounded">token</code>，
            复制其值粘贴到下方。
          </p>
        </div>
        <div className="flex gap-4 items-end">
          <div className="flex-1">
            <label className="text-xs font-medium mb-1 block text-muted-foreground">Token (必填)</label>
            <input 
              type="text"
              value={token} 
              onChange={e => setToken(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono" 
              placeholder="eyJ..." 
            />
          </div>
          <div className="w-48">
            <label className="text-xs font-medium mb-1 block text-muted-foreground">邮箱 (选填)</label>
            <input 
              type="text" 
              value={email} 
              onChange={e => setEmail(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" 
              placeholder="可留空 (或输入密语解锁)" 
            />
          </div>
          <div className="w-48">
            <label className="text-xs font-medium mb-1 block text-muted-foreground">密码 (选填)</label>
            <input 
              type="password" 
              value={password} 
              onChange={e => setPassword(e.target.value)} 
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" 
              placeholder="用于自愈 / 或密语" 
            />
          </div>
          <Button onClick={handleAdd} variant="secondary" className="h-10">
            <Plus className="mr-2 h-4 w-4" /> 注入
          </Button>
        </div>
      </div>

      <div className="rounded-xl border bg-card overflow-hidden">
        <table className="w-full text-sm text-left">
          <thead className="bg-muted/50 border-b text-muted-foreground">
            <tr>
              <th className="h-12 px-4 align-middle font-medium">账号</th>
              <th className="h-12 px-4 align-middle font-medium">状态</th>
              <th className="h-12 px-4 align-middle font-medium">正在处理请求数</th>
              <th className="h-12 px-4 align-middle font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {accounts.length === 0 && (
              <tr>
                <td colSpan={4} className="p-4 text-center text-muted-foreground">暂无账号数据</td>
              </tr>
            )}
            {accounts.map(acc => (
              <tr key={acc.email} className="border-b transition-colors hover:bg-muted/50">
                <td className="p-4 align-middle font-medium">{acc.email}</td>
                <td className="p-4 align-middle">
                  {acc.valid ? (
                    <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-green-100 text-green-800">
                      Token 有效
                    </span>
                  ) : (
                    <span className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold bg-red-100 text-red-800">
                      失效/待刷新
                    </span>
                  )}
                </td>
                <td className="p-4 align-middle">{acc.inflight} 并发</td>
                <td className="p-4 align-middle text-right space-x-2">
                  {!acc.valid && (
                    <Button variant="outline" size="sm" onClick={() => handleActivate(acc.email)} className="text-orange-600 border-orange-200 hover:bg-orange-50">
                      <MailWarning className="h-4 w-4 mr-1" /> 激活
                    </Button>
                  )}
                  <Button variant="outline" size="sm" onClick={() => handleVerify(acc.email)} disabled={verifying === acc.email} title="强制验活">
                    {verifying === acc.email ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => handleDelete(acc.email)} className="text-destructive hover:bg-destructive/10 hover:text-destructive" title="删除">
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
