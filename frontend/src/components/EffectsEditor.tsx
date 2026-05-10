import { useRef } from 'react'
import { useI18n } from '../i18n/I18nProvider'

/**
 * Two-column editor for activity / item effects.
 *
 * Left: textarea where each line is `key: value`. Right: clickable stats
 * (numeric `_change` keys derived from character templates) and moods
 * (from shared/config/moods.json). Clicking a stat appends
 * `<stat>_change: ` to the textarea on a fresh line; clicking a mood
 * appends `mood_influence: <id>`.
 *
 * The lists are loaded once per mount; if either fetch fails we fall
 * back to a hard-coded canonical set so the editor stays useful even
 * without the optional endpoints.
 */
const FALLBACK_STATS = [
  'stamina',
  'courage',
  'stress',
  'lust',
  'attention',
  'inhibition',
  'submission',
  'popularity',
  'trustworthiness',
]

const FALLBACK_MOODS = [
  'pleased',
  'happy',
  'relaxed',
  'refreshed',
  'creative',
  'chatty',
  'exuberant',
  'euphoric',
  'exhausted',
  'drunk',
  'sweating',
]

export function EffectsEditor({
  value,
  onChange,
  rows = 4,
  placeholder,
}: {
  value: string
  onChange: (next: string) => void
  rows?: number
  placeholder?: string
}) {
  const { t } = useI18n()
  // Hard-coded canonical lists — stats come from the character templates
  // and moods from shared/config/moods.json. Users can still type any
  // custom key directly in the textarea; the chips are quick-insert
  // helpers, not validation. If the canonical lists drift, update these.
  const stats = FALLBACK_STATS
  const moods = FALLBACK_MOODS
  const taRef = useRef<HTMLTextAreaElement | null>(null)

  const appendLine = (line: string) => {
    const cur = value.replace(/\s+$/, '')
    const next = cur ? `${cur}\n${line}` : line
    onChange(next)
    // Move caret to end of the appended line so the user can immediately type.
    requestAnimationFrame(() => {
      const ta = taRef.current
      if (!ta) return
      ta.focus()
      ta.selectionStart = ta.selectionEnd = next.length
    })
  }

  const usedStats = new Set<string>()
  const usedMoods = new Set<string>()
  for (const line of value.split('\n')) {
    const m = line.match(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:/)
    if (!m) continue
    if (m[1].endsWith('_change')) usedStats.add(m[1].slice(0, -'_change'.length))
    else if (m[1] === 'mood_influence') {
      const v = line.split(':', 2)[1]?.trim().toLowerCase()
      if (v) usedMoods.add(v)
    }
  }

  return (
    <div className="ga-effects-editor">
      <textarea
        ref={taRef}
        className="ga-textarea ga-effects-textarea"
        rows={rows}
        value={value}
        placeholder={placeholder ?? 'stamina_change: -10\ncourage_change: 5\nmood_influence: relaxed'}
        onChange={(e) => onChange(e.target.value)}
        style={{ fontFamily: 'monospace' }}
      />
      <div className="ga-effects-helpers">
        <div className="ga-effects-helper">
          <div className="ga-effects-helper-title">{t('Stats')}</div>
          <div className="ga-effects-helper-list">
            {stats.map((s) => (
              <button
                key={s}
                type="button"
                className={`ga-effects-chip${usedStats.has(s) ? ' is-used' : ''}`}
                onClick={() => appendLine(`${s}_change: `)}
                title={t('Append "{key}_change: " line').replace('{key}', s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <div className="ga-effects-helper">
          <div className="ga-effects-helper-title">{t('Moods')}</div>
          <div className="ga-effects-helper-list">
            {moods.map((m) => (
              <button
                key={m}
                type="button"
                className={`ga-effects-chip${usedMoods.has(m) ? ' is-used' : ''}`}
                onClick={() => appendLine(`mood_influence: ${m}`)}
                title={t('Append "mood_influence: {id}" line').replace('{id}', m)}
              >
                {m}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
