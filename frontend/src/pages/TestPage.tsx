import { useState } from "react"
import { Button } from "../components/ui/button"
import { Send, RefreshCw, Bot } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { toast } from "sonner"

export default function TestPage() {
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([])
  const [input, setInput] = useState("")
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState("qwen3.6-plus")
  const [stream, setStream] = useState(true)

  const handleSend = async () => {
    if (!input.trim() || loading) return
    const userMsg = { role: "user", content: input }
    setMessages(prev => [...prev, userMsg])
    setInput("")
    setLoading(true)

    try {
      if (!stream) {
        const res = await fetch("/v1/chat/completions", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({
            model,
            messages: [...messages, userMsg],
            stream: false
          })
        })
        
        const data = await res.json()
        if (data.choices && data.choices[0]) {
          setMessages(prev => [...prev, data.choices[0].message])
        } else {
          toast.error("请求失败，请检查账号池和余额")
          setMessages(prev => [...prev, { role: "assistant", content: `❌ 请求失败: ${JSON.stringify(data)}` }])
        }
      } else {
        // Stream handling
        const res = await fetch("/v1/chat/completions", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({
            model,
            messages: [...messages, userMsg],
            stream: true
          })
        })
        if (!res.body) throw new Error("No body")
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        setMessages(prev => [...prev, { role: "assistant", content: "" }])
        
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const chunk = decoder.decode(value)
          const lines = chunk.split("\n")
          for (let line of lines) {
            line = line.trim()
            if (!line || line === "data: [DONE]") continue
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6))
                const content = data.choices[0]?.delta?.content || ""
                setMessages(prev => {
                  const newMsgs = [...prev]
                  newMsgs[newMsgs.length - 1].content += content
                  return newMsgs
                })
              } catch(e) {}
            }
          }
        }
      }
    } catch (err: any) {
      toast.error("网络错误")
      setMessages(prev => [...prev, { role: "assistant", content: `❌ 网络错误: ${err.message}` }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-[calc(100vh-10rem)] space-y-4 max-w-5xl mx-auto">
      <div className="flex justify-between items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight">接口测试</h2>
          <p className="text-muted-foreground">在此测试您的 API 分发是否正常工作。</p>
        </div>
        <div className="flex gap-4 items-center">
          <div className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md">
            <span className="font-medium text-muted-foreground">模型:</span>
            <select value={model} onChange={e => setModel(e.target.value)} className="bg-transparent font-mono outline-none">
              <option value="qwen3.6-plus">qwen3.6-plus</option>
              <option value="qwen-max">qwen-max</option>
              <option value="qwen-turbo">qwen-turbo</option>
            </select>
          </div>
          <div className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md cursor-pointer" onClick={() => setStream(!stream)}>
            <input type="checkbox" checked={stream} onChange={() => {}} className="cursor-pointer" />
            <span className="font-medium">流式传输 (Stream)</span>
          </div>
          <Button variant="outline" onClick={() => setMessages([])}>
            <RefreshCw className="mr-2 h-4 w-4" /> 清空对话
          </Button>
        </div>
      </div>

      <div className="flex-1 rounded-xl border bg-card overflow-hidden flex flex-col shadow-sm">
        <div className="flex-1 overflow-y-auto p-6 space-y-6 flex flex-col">
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-muted-foreground space-y-4">
              <Bot className="h-12 w-12 text-muted-foreground/30" />
              <p className="text-sm">发送一条消息以开始测试，系统将通过 /v1/chat/completions 进行调用。</p>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[80%] rounded-xl px-4 py-3 text-sm shadow-sm ${msg.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted/30 border text-foreground"}`}>
                <div className="whitespace-pre-wrap leading-relaxed">{msg.content}</div>
              </div>
            </div>
          ))}
          {loading && !stream && (
            <div className="flex justify-start">
              <div className="max-w-[80%] rounded-xl px-4 py-3 text-sm shadow-sm bg-muted/30 border text-foreground">
                <span className="animate-pulse flex items-center gap-2"><Bot className="h-4 w-4" /> 思考中...</span>
              </div>
            </div>
          )}
        </div>
        
        <div className="p-4 border-t bg-muted/30 flex gap-3 items-center">
          <input 
            type="text" 
            value={input} 
            onChange={e => setInput(e.target.value)} 
            onKeyDown={e => e.key === "Enter" && handleSend()}
            className="flex h-12 w-full rounded-md border border-input bg-background px-4 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50" 
            placeholder="输入测试消息..." 
            disabled={loading}
          />
          <Button onClick={handleSend} disabled={loading || !input.trim()} className="h-12 px-6">
            <Send className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
