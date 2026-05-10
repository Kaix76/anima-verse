import { useState } from 'react'
import { useI18n } from '../i18n/I18nProvider'

/**
 * Tiny emoji picker for state icons. Shows a text field plus a popover
 * grid with the icons most commonly used for states / conditions /
 * moods. The user can also paste any other emoji directly into the
 * field — the picker is a shortcut, not a constraint.
 */
const SUGGESTED_ICONS = [
  '🍺', '🍷', '🍸', '🥃', '☕', '🍵', '🥛', '🚬', '💊',
  '😀', '😎', '😴', '😵', '🥴', '🤒', '🤕', '🤧', '😭', '😡',
  '😱', '🤔', '😈', '🤯', '🤤', '🥵', '🥶', '😍', '🥺', '🤗',
  '💪', '🤸', '🏃', '🛌', '🍳', '🛁', '💃', '🎉', '🎵', '🎮',
  '⚡', '🔥', '💧', '❄️', '☀️', '🌙', '⭐', '✨', '💫', '🌈',
  '❤️', '💔', '💯', '🎯', '🎲', '🎁', '💎', '🔑', '⚠️', '🚫',
]

export function IconPicker({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)

  return (
    <div className="ga-icon-picker">
      <input
        className="ga-input ga-icon-picker-input"
        value={value}
        placeholder="🍺"
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        type="button"
        className="ga-btn ga-btn-sm"
        onClick={() => setOpen((v) => !v)}
        title={t('Choose an emoji')}
      >
        😀
      </button>
      {open ? (
        <div className="ga-icon-picker-popover" role="listbox">
          <div className="ga-icon-picker-grid">
            {SUGGESTED_ICONS.map((ic) => (
              <button
                key={ic}
                type="button"
                className={`ga-icon-picker-cell${ic === value ? ' is-selected' : ''}`}
                onClick={() => {
                  onChange(ic)
                  setOpen(false)
                }}
              >
                {ic}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="ga-btn ga-btn-sm"
            style={{ marginTop: 6 }}
            onClick={() => {
              onChange('')
              setOpen(false)
            }}
          >
            {t('Clear')}
          </button>
        </div>
      ) : null}
    </div>
  )
}
