import { useState } from "react"
import { Image as ImageIcon, RefreshCw, Download, Wand2 } from "lucide-react"
import { Button } from "../components/ui/button"
import { toast } from "sonner"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { useTranslation } from "react-i18next"

const ASPECT_RATIOS = [
  { label: "1:1",  value: "1:1",   w: 1024, h: 1024 },
  { label: "16:9", value: "16:9",  w: 1024, h: 576  },
  { label: "9:16", value: "9:16",  w: 576,  h: 1024 },
  { label: "4:3",  value: "4:3",   w: 1024, h: 768  },
  { label: "3:4",  value: "3:4",   w: 768,  h: 1024 },
]

interface GeneratedImage {
  url: string
  revised_prompt: string
  ratio: string
}

export default function ImagePage() {
  const { t } = useTranslation()
  const [prompt, setPrompt] = useState("")
  const [ratio, setRatio] = useState("1:1")
  const [n, setN] = useState(1)
  const [loading, setLoading] = useState(false)
  const [images, setImages] = useState<GeneratedImage[]>([])
  const [error, setError] = useState<string | null>(null)

  const selectedRatio = ASPECT_RATIOS.find(r => r.value === ratio)!
  const sizeStr = `${selectedRatio.w}x${selectedRatio.h}`

  const handleGenerate = async () => {
    if (!prompt.trim() || loading) return
    setLoading(true)
    setError(null)

    try {
      const res = await fetch(`${API_BASE}/v1/images/generations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify({
          model: "dall-e-3",
          prompt: prompt.trim(),
          n,
          size: sizeStr,
          response_format: "url",
        }),
      })

      const data = await res.json()
      if (!res.ok) {
        const detail = data?.detail || data?.error || `HTTP ${res.status}`
        setError(String(detail))
        toast.error(t("images.generationFailed", { detail: String(detail).slice(0, 80) }))
        return
      }

      const newImages: GeneratedImage[] = (data.data || []).map((item: any) => ({
        url: item.url,
        revised_prompt: item.revised_prompt || prompt,
        ratio,
      }))

      if (newImages.length === 0) {
        setError(t("images.noImagesReturned"))
        toast.error(t("images.noImagesReturned"))
        return
      }

      setImages(prev => [...newImages, ...prev])
      toast.success(t("images.generatedSuccess", { n: newImages.length }))
    } catch (err: any) {
      const msg = err.message || t("images.networkError")
      setError(msg)
      toast.error(t("images.generationFailed", { detail: msg }))
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = (url: string, idx: number) => {
    const a = document.createElement("a")
    a.href = url
    a.download = `qwen_image_${Date.now()}_${idx}.png`
    a.target = "_blank"
    a.rel = "noopener noreferrer"
    a.click()
  }

  return (
    <div className="space-y-6 max-w-5xl">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">{t("images.title")}</h2>
        <p className="text-muted-foreground">{t("images.subtitle")}</p>
      </div>

      {/* Input area */}
      <div className="rounded-xl border bg-card shadow-sm p-6 space-y-4">
        <div className="space-y-2">
          <label className="text-sm font-medium">{t("images.promptLabel")}</label>
          <textarea
            rows={3}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder={t("images.promptPlaceholder")}
            className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm resize-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            disabled={loading}
            onKeyDown={e => {
              if (e.key === "Enter" && e.ctrlKey) handleGenerate()
            }}
          />
          <p className="text-xs text-muted-foreground">{t("images.promptTip")}</p>
        </div>

        <div className="flex flex-wrap gap-4 items-end">
          {/* Ratio */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">{t("images.ratioLabel")}</label>
            <div className="flex gap-2">
              {ASPECT_RATIOS.map(r => (
                <button
                  key={r.value}
                  onClick={() => setRatio(r.value)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    ratio === r.value
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>

          {/* Count */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">{t("images.countLabel")}</label>
            <div className="flex gap-2">
              {[1, 2, 4].map(v => (
                <button
                  key={v}
                  onClick={() => setN(v)}
                  className={`px-3 py-1.5 rounded-md text-sm font-medium border transition-all ${
                    n === v
                      ? "bg-primary text-primary-foreground border-primary shadow-sm"
                      : "bg-background border-border text-muted-foreground hover:text-foreground hover:border-foreground/30"
                  }`}
                  disabled={loading}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>

          {/* Size preview */}
          <div className="text-xs text-muted-foreground font-mono bg-muted/50 border rounded-md px-2 py-1">
            {sizeStr}
          </div>

          {/* Generate button */}
          <Button
            onClick={handleGenerate}
            disabled={loading || !prompt.trim()}
            className="ml-auto h-10 px-6 gap-2"
          >
            {loading
              ? <><RefreshCw className="h-4 w-4 animate-spin" /> {t("images.generating")}</>
              : <><Wand2 className="h-4 w-4" /> {t("images.generate")}</>
            }
          </Button>
        </div>

        {/* Error */}
        {error && (
          <div className="rounded-md bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 text-sm">
            {error}
          </div>
        )}
      </div>

      {/* Loading state */}
      {loading && (
        <div className="rounded-xl border bg-card shadow-sm p-8">
          <div className="flex flex-col items-center justify-center gap-4 text-muted-foreground">
            <div className="relative">
              <ImageIcon className="h-16 w-16 text-muted-foreground/20" />
              <RefreshCw className="h-6 w-6 animate-spin absolute -bottom-1 -right-1 text-primary" />
            </div>
            <div className="text-center">
              <p className="font-medium">{t("images.loadingState")}</p>
              <p className="text-sm text-muted-foreground/70 mt-1">{t("images.loadingHint")}</p>
            </div>
          </div>
        </div>
      )}

      {/* Image grid */}
      {images.length > 0 && !loading && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold">{t("images.resultsTitle", { n: images.length })}</h3>
            <Button variant="ghost" size="sm" onClick={() => setImages([])}>
              {t("images.clearAll")}
            </Button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {images.map((img, idx) => (
              <div key={`${img.url}-${idx}`} className="rounded-xl border bg-card shadow-sm overflow-hidden group">
                <div className="relative bg-muted/30">
                  <img
                    src={img.url}
                    alt={img.revised_prompt}
                    className="w-full h-auto object-contain"
                    loading="lazy"
                    onError={e => {
                      const target = e.currentTarget
                      target.style.display = "none"
                      target.nextElementSibling?.classList.remove("hidden")
                    }}
                  />
                  <div className="hidden items-center justify-center p-8 text-muted-foreground text-sm">
                    <ImageIcon className="h-8 w-8 mr-2" /> {t("images.imageLoadFailed")}
                  </div>
                  {/* Hover bar */}
                  <div className="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-3">
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => handleDownload(img.url, idx)}
                      className="gap-1.5"
                    >
                      <Download className="h-3.5 w-3.5" /> {t("images.download")}
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => window.open(img.url, "_blank")}
                    >
                      {t("images.openInNewWindow")}
                    </Button>
                  </div>
                </div>
                <div className="p-3 space-y-1">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="bg-muted rounded px-1.5 py-0.5 font-mono">{img.ratio}</span>
                    <span className="truncate">{img.revised_prompt.slice(0, 80)}</span>
                  </div>
                  <div className="text-xs text-muted-foreground font-mono truncate">{img.url}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty */}
      {images.length === 0 && !loading && (
        <div className="rounded-xl border bg-card/50 shadow-sm p-12">
          <div className="flex flex-col items-center gap-4 text-muted-foreground">
            <ImageIcon className="h-16 w-16 text-muted-foreground/20" />
            <div className="text-center">
              <p className="font-medium">{t("images.emptyTitle")}</p>
              <p className="text-sm text-muted-foreground/70 mt-1">{t("images.emptyHint")}</p>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
