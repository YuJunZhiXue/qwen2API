import i18n from "i18next"
import LanguageDetector from "i18next-browser-languagedetector"
import { initReactI18next } from "react-i18next"

import en from "./locales/en.json"
import ptBR from "./locales/pt-BR.json"

export const SUPPORTED_LNGS = ["en", "pt-BR"] as const
export type SupportedLng = (typeof SUPPORTED_LNGS)[number]

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      "pt-BR": { translation: ptBR },
    },
    fallbackLng: "en",
    supportedLngs: SUPPORTED_LNGS as unknown as string[],
    interpolation: { escapeValue: false },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      caches: ["localStorage"],
      lookupLocalStorage: "qwen2api_lang",
    },
  })

export default i18n
