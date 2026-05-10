import { useCallback, useEffect, useState } from 'react'
import { useI18n } from './i18n/I18nProvider'
import { TABS, isTabId, type TabId } from './tabs/registry'
import { ErrorBoundary } from './components/ErrorBoundary'

function readHashTab(): TabId {
  const raw = window.location.hash.replace(/^#\/?/, '').toLowerCase()
  return isTabId(raw) ? raw : 'activities'
}

export default function App() {
  const { t } = useI18n()
  const [active, setActive] = useState<TabId>(readHashTab)

  useEffect(() => {
    const onHashChange = () => setActive(readHashTab())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const select = useCallback((id: TabId) => {
    window.location.hash = `#/${id}`
    setActive(id)
  }, [])

  const ActiveComponent = TABS.find((tab) => tab.id === active)?.Component

  return (
    <div className="ga-shell">
      <header className="ga-header">
        <a className="ga-back" href="/" title={t('Back to chat')}>
          ← {t('Back to chat')}
        </a>
        <h1>{t('Game Admin')}</h1>
      </header>
      <nav className="ga-tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={active === tab.id}
            className={`ga-tab${active === tab.id ? ' is-active' : ''}`}
            onClick={() => select(tab.id)}
          >
            {t(tab.label)}
          </button>
        ))}
      </nav>
      <main className="ga-body">
        {ActiveComponent ? (
          <ErrorBoundary key={active}>
            <ActiveComponent />
          </ErrorBoundary>
        ) : (
          <div className="ga-placeholder">{t('Unknown tab')}</div>
        )}
      </main>
    </div>
  )
}
