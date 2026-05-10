import { useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/I18nProvider'

/**
 * Body silhouette built around a hand-drawn croquis SVG asset
 * (`public/silhouette.svg`, served at `/static/game_admin/silhouette.svg`
 * after build). Slot region overlays sit in a second SVG sharing the
 * same viewBox so coordinates align.
 *
 * Live editing: click the 🛠 toggle in the top right corner to open a
 * panel with one textarea per slot. Edits render immediately and persist
 * to `localStorage["ga.silhouetteSlotPaths"]` so they survive reloads.
 * "Copy as code" exports the TypeScript literal you can paste back into
 * `DEFAULT_SLOT_PATHS` in this file once the layout is right.
 */

const COLOR_SLOT = 'rgba(31, 113, 235, 0.8)'
const COLOR_COVER = 'rgb(45, 45, 45)'
const COLOR_PARTIAL = 'rgba(80, 80, 80, 0.45)'

const STORAGE_KEY = 'ga.silhouetteSlotPaths'

type SlotPathMap = Record<string, string>

const DEFAULT_SLOT_PATHS: SlotPathMap = {
  // Head — oval covering the cranium.
  head: 'M 448 20 a 70 90 0 1 0 0.1 0 Z',

  // Neck — between jaw and collarbone.
  neck: 'M 420 180 L 480 180 L 520 220 L 380 220 Z',

  // Outer (jacket envelope) — shoulders down to mid-torso.
  outer: 'M 295 235 Q 360 190 450 240 Q 540 190 605 235 L 605 680 Q 540 605 450 605 Q 360 605 295 680 Z',

  // Top (chest panel).
  top: 'M 350 260 Q 380 225 450 225 Q 520 225 540 260 L 545 520 Q 510 565 450 565 Q 390 565 350 520 Z',

  // Underwear top (bra) — two breast circles.
  underwear_top:
    'M 445 300 a 105 50 0 1 0 0.1 0 Z ',

  // Bottom (briefs) — hip area.
  bottom: 'M 340 580 Q 380 625 450 625 Q 520 625 560 580 L 580 720 Q 510 740 450 740 Q 390 740 320 720 Z',

  // Underwear bottom — narrower triangle inside the briefs.
  underwear_bottom: 'M 340 600 Q 425 630 450 632 Q 475 630 560 600 L 480 720 Q 460 728 450 728 Q 440 728 420 720 Z',

  // Legs — two limbs (full thigh-to-ankle).
  legs:
    'M 320 730 Q 380 745 440 740 L 446 1085 L 410 1085 Q 392 1085 380 1080 Q 350 950 320 730 Z' +
    'M 580 730 Q 520 745 460 740 L 454 1085 L 490 1085 Q 508 1085 520 1080 Q 550 950 580 730 Z',

  // Feet — wedges below the ankles.
  feet:
    'M 440 1090 L 400 1090 L 350 1200 L 440 1190 Z' +
    'M 460 1090 L 500 1090 L 550 1200 L 460 1190 Z',
}

const SLOT_ORDER_PAINT = [
  'outer',
  'top',
  'bottom',
  'legs',
  'underwear_top',
  'underwear_bottom',
  'head',
  'neck',
  'feet',
]

function loadOverrides(): SlotPathMap {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const obj = JSON.parse(raw)
    return obj && typeof obj === 'object' ? (obj as SlotPathMap) : {}
  } catch {
    return {}
  }
}

function saveOverrides(map: SlotPathMap) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch {
    /* ignore */
  }
}

function colorFor(slot: string, slotSet: Set<string>, coverSet: Set<string>, partialSet: Set<string>) {
  if (slotSet.has(slot)) return COLOR_SLOT
  if (coverSet.has(slot)) return COLOR_COVER
  if (partialSet.has(slot)) return COLOR_PARTIAL
  return null
}

export type SlotStatus = 'slot' | 'cover' | 'partial' | 'empty'

export function Silhouette({
  slots,
  covers,
  partially_covers,
  onCycleSlot,
}: {
  slots: string[]
  covers: string[]
  partially_covers: string[]
  /** When provided, slot regions become clickable and cycle through
   *  empty → slot → cover → partial → empty. */
  onCycleSlot?: (slot: string) => void
}) {
  const { t } = useI18n()
  const slotSet = new Set(slots)
  const coverSet = new Set(covers)
  const partialSet = new Set(partially_covers)

  const [overrides, setOverrides] = useState<SlotPathMap>(loadOverrides)
  const [editing, setEditing] = useState(false)

  // Effective path map — defaults overlaid by user overrides.
  const paths = useMemo<SlotPathMap>(() => ({ ...DEFAULT_SLOT_PATHS, ...overrides }), [overrides])

  useEffect(() => {
    saveOverrides(overrides)
  }, [overrides])

  const setPath = (slot: string, value: string) => {
    setOverrides((prev) => {
      const next = { ...prev }
      if (value === DEFAULT_SLOT_PATHS[slot] || value.trim() === '') delete next[slot]
      else next[slot] = value
      return next
    })
  }

  const resetSlot = (slot: string) => {
    setOverrides((prev) => {
      const next = { ...prev }
      delete next[slot]
      return next
    })
  }

  const resetAll = () => {
    setOverrides({})
  }

  const copyAsCode = () => {
    const lines = ['const SLOT_PATHS: Record<string, string> = {']
    for (const slot of Object.keys(DEFAULT_SLOT_PATHS)) {
      const value = paths[slot]
      const escaped = value.replace(/'/g, "\\'")
      lines.push(`  ${slot}: '${escaped}',`)
    }
    lines.push('}')
    const code = lines.join('\n')
    if (navigator.clipboard) {
      navigator.clipboard.writeText(code).catch(() => undefined)
    }
    // eslint-disable-next-line no-console
    console.log('[silhouette] paths:\n' + code)
  }

  return (
    <div className="ga-silhouette">
      <div className="ga-silhouette-figure">
        <img
          className="ga-silhouette-img"
          src="/static/game_admin/silhouette.svg"
          alt=""
          draggable={false}
        />
        <svg
          className="ga-silhouette-overlay"
          viewBox="240 0 410 1216"
          preserveAspectRatio="xMidYMid meet"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          {SLOT_ORDER_PAINT.map((slot) => {
            const fill = colorFor(slot, slotSet, coverSet, partialSet)
            const interactive = !!onCycleSlot
            // Inactive slots stay invisible by default. In edit mode and
            // in interactive (click-to-cycle) mode we paint them faintly
            // so the user can see / aim at the regions.
            let effectiveFill = fill
            if (!fill && (editing || interactive)) {
              effectiveFill = 'rgba(180, 188, 200, 0.10)'
            }
            if (!effectiveFill) return null
            return (
              <path
                key={slot}
                d={paths[slot] || ''}
                fill={effectiveFill}
                stroke={editing ? 'rgba(255, 255, 255, 0.5)' : 'rgba(255, 255, 255, 0.18)'}
                strokeWidth={2}
                strokeLinejoin="round"
                className={interactive ? 'ga-silhouette-slot-hit' : undefined}
                onClick={interactive ? () => onCycleSlot(slot) : undefined}
              >
                <title>
                  {slot}
                  {interactive ? ` — ${t('click to cycle')}` : ''}
                </title>
              </path>
            )
          })}
        </svg>
        <button
          type="button"
          className={`ga-silhouette-edit-toggle${editing ? ' is-on' : ''}`}
          onClick={() => setEditing((v) => !v)}
          title={editing ? t('Close slot editor') : t('Edit slot paths')}
        >
          🛠
        </button>
      </div>
      <div className="ga-silhouette-legend">
        <span><i className="lg-slot" />{t('slot')}</span>
        <span><i className="lg-cover" />{t('covers')}</span>
        <span><i className="lg-partial" />{t('partial')}</span>
      </div>
      {editing ? (
        <SilhouetteEditor
          paths={paths}
          overrides={overrides}
          onChange={setPath}
          onReset={resetSlot}
          onResetAll={resetAll}
          onCopy={copyAsCode}
        />
      ) : null}
    </div>
  )
}

interface EditorProps {
  paths: SlotPathMap
  overrides: SlotPathMap
  onChange: (slot: string, value: string) => void
  onReset: (slot: string) => void
  onResetAll: () => void
  onCopy: () => void
}

function SilhouetteEditor({ paths, overrides, onChange, onReset, onResetAll, onCopy }: EditorProps) {
  const { t } = useI18n()
  return (
    <div className="ga-silhouette-editor">
      <div className="ga-silhouette-editor-header">
        <strong>{t('Slot paths')}</strong>
        <span className="ga-form-hint">
          viewBox <code>240 0 410 1216</code>
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button className="ga-btn ga-btn-sm" onClick={onCopy} title={t('Copy as TypeScript object')}>
            {t('Copy code')}
          </button>
          <button className="ga-btn ga-btn-sm ga-btn-danger" onClick={onResetAll}>
            {t('Reset all')}
          </button>
        </div>
      </div>
      <div className="ga-silhouette-editor-list">
        {Object.keys(DEFAULT_SLOT_PATHS).map((slot) => {
          const isOverridden = slot in overrides
          return (
            <div key={slot} className="ga-silhouette-editor-row">
              <div className="ga-silhouette-editor-label">
                <code>{slot}</code>
                {isOverridden ? (
                  <button
                    className="ga-btn ga-btn-sm"
                    onClick={() => onReset(slot)}
                    title={t('Restore default')}
                  >
                    ↺
                  </button>
                ) : null}
              </div>
              <textarea
                className="ga-textarea"
                rows={2}
                value={paths[slot] || ''}
                onChange={(e) => onChange(slot, e.target.value)}
                style={{
                  fontFamily: 'monospace',
                  fontSize: 11,
                  borderColor: isOverridden ? 'var(--primary, #1f6feb)' : undefined,
                }}
                spellCheck={false}
              />
            </div>
          )
        })}
      </div>
      <div className="ga-form-hint" style={{ marginTop: 4 }}>
        {t(
          'Edits live-update the diagram and persist to localStorage. "Copy code" puts a TypeScript literal on the clipboard you can paste into DEFAULT_SLOT_PATHS in components/Silhouette.tsx.',
        )}
      </div>
    </div>
  )
}
