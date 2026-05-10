/**
 * Bridge to the server-side i18n contract:
 *   GET /i18n/translations/<lang>  →  { lang, translations: { "English source": "Übersetzung", ... } }
 *
 * The active language is read from a cookie / localStorage / URL param the
 * main app already manages. For the Game-Admin page we mirror that contract
 * so a language switch in the main app is visible after a reload here.
 *
 * Source strings stay English in the React code. Missing keys log once per
 * (lang, source) pair so the i18n welle can pick them up — same convention
 * as the legacy `t()` in static/script.js.
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

type TranslationsMap = Record<string, string>

interface I18nValue {
  lang: string
  t: (en: string) => string
  setLang: (lang: string) => void
}

const I18nContext = createContext<I18nValue | null>(null)

const missingLogged = new Set<string>()

function readInitialLang(): string {
  // Match the conventions used elsewhere in the app: ?lang= overrides,
  // otherwise localStorage('uiLang'), otherwise 'en'.
  if (typeof window === 'undefined') return 'en'
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get('lang')
  if (fromUrl) return fromUrl
  const fromStorage = window.localStorage.getItem('uiLang')
  if (fromStorage) return fromStorage
  return 'en'
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<string>(readInitialLang)
  const [translations, setTranslations] = useState<TranslationsMap>({})

  useEffect(() => {
    let cancelled = false
    if (lang === 'en') {
      setTranslations({})
      return
    }
    fetch(`/i18n/translations/${encodeURIComponent(lang)}`)
      .then((res) => (res.ok ? res.json() : { translations: {} }))
      .then((data) => {
        if (cancelled) return
        setTranslations(data.translations || {})
      })
      .catch(() => {
        if (cancelled) return
        setTranslations({})
      })
    return () => {
      cancelled = true
    }
  }, [lang])

  const value = useMemo<I18nValue>(() => {
    return {
      lang,
      setLang: (next) => {
        try {
          window.localStorage.setItem('uiLang', next)
        } catch {
          /* ignore */
        }
        setLangState(next)
      },
      t: (en) => {
        if (!en) return en
        if (lang === 'en') return en
        const hit = translations[en]
        if (hit) return hit
        const k = `${lang}::${en}`
        if (!missingLogged.has(k)) {
          missingLogged.add(k)
          // eslint-disable-next-line no-console
          console.debug(`[i18n] missing [${lang}]: ${en}`)
        }
        return en
      },
    }
  }, [lang, translations])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext)
  if (!ctx) throw new Error('useI18n must be used inside <I18nProvider>')
  return ctx
}
