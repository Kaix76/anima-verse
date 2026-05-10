import type { ReactNode } from 'react'
import { useI18n } from '../i18n/I18nProvider'

/**
 * Sticky action bar at the top of the detail column. Save, Cancel and
 * Delete (or any extra) live here so they stay visible while the user
 * scrolls through long forms.
 *
 *   <DetailToolbar onSave={save} onCancel={cancel} onDelete={remove} />
 *
 * Pass `disabled` to gate Save (e.g. while saving). `extra` slots in
 * additional context-specific buttons (e.g. "Remove override"). `title`
 * shows up on the left so the user always sees what's being edited.
 */
/**
 * `storage` reflects where the entry currently lives. The Move button
 * sends the entry to the OPPOSITE store and the label adapts:
 *   storage=world  → "Move to shared"
 *   storage=shared → "Move to world"
 */
export type Storage = 'world' | 'shared' | 'world override'

export function DetailToolbar({
  title,
  saveLabel,
  cancelLabel,
  deleteLabel,
  onSave,
  onCancel,
  onDelete,
  onMove,
  storage,
  disabled,
  extra,
}: {
  title?: ReactNode
  saveLabel?: string
  cancelLabel?: string
  deleteLabel?: string
  onSave?: () => void
  onCancel?: () => void
  onDelete?: () => void
  onMove?: (target: 'world' | 'shared') => void
  storage?: Storage
  disabled?: boolean
  extra?: ReactNode
}) {
  const { t } = useI18n()
  const isShared = storage === 'shared'
  const moveTarget: 'world' | 'shared' = isShared ? 'world' : 'shared'
  const moveLabel = isShared ? t('Move to world') : t('Move to shared')
  return (
    <div className="ga-detail-toolbar">
      {onSave ? (
        <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={onSave} disabled={disabled}>
          {saveLabel ?? t('Save')}
        </button>
      ) : null}
      {onCancel ? (
        <button className="ga-btn ga-btn-sm" onClick={onCancel} disabled={disabled}>
          {cancelLabel ?? t('Cancel')}
        </button>
      ) : null}
      {extra}
      {onMove ? (
        <button
          className="ga-btn ga-btn-sm"
          onClick={() => onMove(moveTarget)}
          disabled={disabled}
          title={moveLabel}
        >
          {moveLabel}
        </button>
      ) : null}
      {onDelete ? (
        <button className="ga-btn ga-btn-danger ga-btn-sm" onClick={onDelete} disabled={disabled}>
          {deleteLabel ?? t('Delete')}
        </button>
      ) : null}
      {title ? <span className="ga-detail-toolbar-title">{title}</span> : null}
    </div>
  )
}
