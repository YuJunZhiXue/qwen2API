import { useState, useEffect } from "react"
import { Globe } from "lucide-react"
import { SUPPORTED_LANGUAGES, getCurrentLanguage, setLanguage } from "../lib/translate"

interface LanguageSelectorProps {
  variant?: "header" | "card"
  className?: string
}

export default function LanguageSelector({ variant = "header", className = "" }: LanguageSelectorProps) {
  const [currentLang, setCurrentLang] = useState<string>(() => getCurrentLanguage())

  useEffect(() => {
    const handleStorage = () => setCurrentLang(getCurrentLanguage())
    window.addEventListener("storage", handleStorage)
    return () => window.removeEventListener("storage", handleStorage)
  }, [])

  const handleChange = (newLang: string) => {
    setCurrentLang(newLang)
    setLanguage(newLang)
  }

  if (variant === "card") {
    return (
      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <label htmlFor="language-card-select" className="text-sm font-medium">
            选择界面语言 / Select Interface Language
          </label>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <div className="relative min-w-[260px]">
            <select
              id="language-card-select"
              value={currentLang}
              onChange={(e) => handleChange(e.target.value)}
              className="admin-input flex h-10 w-full appearance-none px-3 py-2 pr-8 text-sm cursor-pointer"
            >
              {SUPPORTED_LANGUAGES.map((lang) => (
                <option key={lang.code} value={lang.code}>
                  {lang.nativeName} ({lang.name})
                </option>
              ))}
            </select>
            <Globe className="pointer-events-none absolute right-2.5 top-2.5 size-4 text-muted-foreground" />
          </div>
          {currentLang !== "zh-CN" && (
            <button
              type="button"
              onClick={() => handleChange("zh-CN")}
              className="rounded-xl border border-border/80 bg-background/80 px-3 py-2 text-xs font-semibold text-muted-foreground hover:bg-muted hover:text-foreground transition"
            >
              恢复原始语言 (Reset to Chinese)
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className={`relative flex items-center ${className}`}>
      <div className="relative flex items-center">
        <Globe className="pointer-events-none absolute left-2.5 size-4 text-muted-foreground" />
        <select
          aria-label="Select Language"
          value={currentLang}
          onChange={(e) => handleChange(e.target.value)}
          className="h-8 md:h-9 appearance-none rounded-full border border-white/75 bg-card/70 pl-8 pr-7 text-xs font-bold text-muted-foreground shadow-sm hover:border-white hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer transition"
        >
          {SUPPORTED_LANGUAGES.map((lang) => (
            <option key={lang.code} value={lang.code}>
              {lang.nativeName}
            </option>
          ))}
        </select>
        <span className="pointer-events-none absolute right-2.5 text-[9px] text-muted-foreground">▼</span>
      </div>
    </div>
  )
}
