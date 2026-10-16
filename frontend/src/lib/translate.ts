export interface LanguageOption {
  code: string
  name: string
  nativeName: string
}

export const SUPPORTED_LANGUAGES: LanguageOption[] = [
  { code: "zh-CN", name: "Chinese (Simplified)", nativeName: "简体中文" },
  { code: "en", name: "English", nativeName: "English" },
  { code: "es", name: "Spanish", nativeName: "Español" },
  { code: "fr", name: "French", nativeName: "Français" },
  { code: "de", name: "German", nativeName: "Deutsch" },
  { code: "ja", name: "Japanese", nativeName: "日本語" },
  { code: "ko", name: "Korean", nativeName: "한국어" },
  { code: "ru", name: "Russian", nativeName: "Русский" },
  { code: "it", name: "Italian", nativeName: "Italiano" },
  { code: "pt", name: "Portuguese", nativeName: "Português" },
  { code: "ar", name: "Arabic", nativeName: "العربية" },
  { code: "hi", name: "Hindi", nativeName: "हिन्दी" },
  { code: "tr", name: "Turkish", nativeName: "Türkçe" },
  { code: "vi", name: "Vietnamese", nativeName: "Tiếng Việt" },
  { code: "th", name: "Thai", nativeName: "ไทย" },
  { code: "id", name: "Indonesian", nativeName: "Bahasa Indonesia" },
  { code: "zh-TW", name: "Chinese (Traditional)", nativeName: "繁體中文" },
]

export function getCurrentLanguage(): string {
  const match = document.cookie.match(/(?:^|;\s*)googtrans=([^;]+)/)
  if (match) {
    const parts = decodeURIComponent(match[1]).split("/")
    if (parts.length >= 3 && parts[2]) {
      return parts[2]
    }
  }
  return localStorage.getItem("app_language") || "zh-CN"
}

export function setLanguage(langCode: string): void {
  localStorage.setItem("app_language", langCode)

  if (langCode === "zh-CN") {
    // Clear cookie to return to original untranslated page
    document.cookie = "googtrans=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;"
    document.cookie = `googtrans=; expires=Thu, 01 Jan 1970 00:00:00 UTC; domain=${window.location.hostname}; path=/;`
    window.location.reload()
    return
  }

  const cookieVal = `/zh-CN/${langCode}`
  document.cookie = `googtrans=${cookieVal}; path=/;`
  document.cookie = `googtrans=${cookieVal}; domain=${window.location.hostname}; path=/;`

  const combo = document.querySelector<HTMLSelectElement>(".goog-te-combo")
  if (combo) {
    combo.value = langCode
    combo.dispatchEvent(new Event("change"))
  } else {
    window.location.reload()
  }
}

export function reapplyLanguageOnRouteChange(): void {
  const currentLang = getCurrentLanguage()
  if (!currentLang || currentLang === "zh-CN") return

  setTimeout(() => {
    const combo = document.querySelector<HTMLSelectElement>(".goog-te-combo")
    if (combo) {
      combo.value = currentLang
      combo.dispatchEvent(new Event("change"))
    }
  }, 300)
}
