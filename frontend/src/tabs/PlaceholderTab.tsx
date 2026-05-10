import { useI18n } from '../i18n/I18nProvider'

interface Props {
  title: string
  hint?: string
}

export function PlaceholderTab({ title, hint }: Props) {
  const { t } = useI18n()
  return (
    <div className="ga-placeholder">
      <p>
        <strong>{t(title)}</strong>
      </p>
      <p>{t('This tab has not been ported yet.')}</p>
      {hint ? <p style={{ fontSize: '11px', opacity: 0.7 }}>{hint}</p> : null}
    </div>
  )
}
