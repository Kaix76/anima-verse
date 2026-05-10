import { useI18n } from '../i18n/I18nProvider'

/**
 * Standard list header for list-detail tabs: title + "+ New" + "⧉ Copy".
 * Copy is enabled only when something is selected (caller decides).
 */
export function ListHeader({
  title,
  onNew,
  onCopy,
  copyDisabled,
}: {
  title: string
  onNew: () => void
  onCopy?: () => void
  copyDisabled?: boolean
}) {
  const { t } = useI18n()
  return (
    <div className="ga-twocol-header">
      <h3>{title}</h3>
      <div className="ga-twocol-header-actions">
        <button className="ga-btn ga-btn-sm ga-btn-primary" onClick={onNew}>
          + {t('New')}
        </button>
        {onCopy ? (
          <button
            className="ga-btn ga-btn-sm"
            onClick={onCopy}
            disabled={copyDisabled}
            title={t('Duplicate the selected entry')}
          >
            ⧉ {t('Copy')}
          </button>
        ) : null}
      </div>
    </div>
  )
}
