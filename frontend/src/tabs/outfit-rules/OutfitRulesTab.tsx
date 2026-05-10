import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiGet, apiPost, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { AutoTextarea } from '../../components/AutoTextarea'

interface OutfitTypeRule {
  required?: string[]
  description?: string
  default?: boolean
}

type RulesMap = Record<string, OutfitTypeRule>

interface OutfitRulesData {
  outfit_types: RulesMap
  valid_slots: string[]
}

// Display order copied from the legacy SLOT_DISPLAY_ORDER in static/script.js.
// Slots not listed here are appended in their server-provided order.
const SLOT_DISPLAY_ORDER = [
  'head',
  'neck',
  'outer',
  'top',
  'underwear_top',
  'bottom',
  'underwear_bottom',
  'legs',
  'feet',
]

function sortSlots(rawSlots: string[]): string[] {
  return [...rawSlots].sort((a, b) => {
    const ia = SLOT_DISPLAY_ORDER.indexOf(a)
    const ib = SLOT_DISPLAY_ORDER.indexOf(b)
    return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib)
  })
}

function describeRenameUpdated(updated: Record<string, number> | undefined): string {
  const u = updated || {}
  return `locations:${u.locations || 0} rooms:${u.rooms || 0} items:${u.items || 0} activities:${u.activities || 0} chars:${u.character_exceptions || 0}`
}

type StatusKind = 'idle' | 'saving' | 'ok' | 'error'

export function OutfitRulesTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [rules, setRules] = useState<RulesMap>({})
  const [slots, setSlots] = useState<string[]>([])
  const [newType, setNewType] = useState('')
  const [loading, setLoading] = useState(true)
  const [status, setStatus] = useState<{ kind: StatusKind; msg: string }>({ kind: 'idle', msg: '' })

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiGet<OutfitRulesData>('/admin/outfit-rules/data')
      setRules(data.outfit_types || {})
      setSlots(sortSlots(data.valid_slots || []))
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }, [t, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const sortedTypes = useMemo(() => Object.keys(rules).sort(), [rules])

  const addType = useCallback(() => {
    const name = newType.trim().toLowerCase()
    if (!name) return
    if (rules[name]) {
      toast(t('Type already exists'), 'error')
      return
    }
    setRules((prev) => ({ ...prev, [name]: { required: [] } }))
    setNewType('')
  }, [newType, rules, t, toast])

  const deleteType = useCallback(
    (name: string) => {
      if (!window.confirm(t('Delete outfit_type "{name}"?').replace('{name}', name))) return
      setRules((prev) => {
        const next = { ...prev }
        delete next[name]
        return next
      })
    },
    [t],
  )

  const toggleSlot = useCallback((typeName: string, slot: string, checked: boolean) => {
    setRules((prev) => {
      const cur = prev[typeName] || { required: [] }
      const req = new Set(cur.required || [])
      if (checked) req.add(slot)
      else req.delete(slot)
      return { ...prev, [typeName]: { ...cur, required: Array.from(req) } }
    })
  }, [])

  const setDefault = useCallback((typeName: string) => {
    setRules((prev) => {
      const next: RulesMap = {}
      for (const k of Object.keys(prev)) {
        const cur = { ...prev[k] }
        if (k === typeName) cur.default = true
        else delete cur.default
        next[k] = cur
      }
      return next
    })
  }, [])

  const setDescription = useCallback((typeName: string, value: string) => {
    setRules((prev) => {
      const cur = prev[typeName] || { required: [] }
      return { ...prev, [typeName]: { ...cur, description: value } }
    })
  }, [])

  const renameType = useCallback(
    async (oldName: string) => {
      const nn = window.prompt(
        t('New name for "{name}"\n(if it already exists → will be merged):').replace('{name}', oldName),
        oldName,
      )
      if (nn === null) return
      const newName = nn.trim().toLowerCase()
      if (!newName || newName === oldName) return
      const isMerge = !!rules[newName]
      const msg = isMerge
        ? t('Merge "{old}" into existing type "{new}"? All references will be rewritten; slots/description of "{new}" are kept.')
            .replace('{old}', oldName)
            .replace(/\{new\}/g, newName)
        : t('Rename "{old}" → "{new}"? All references (locations, rooms, items, activities, character exceptions) will be rewritten.')
            .replace('{old}', oldName)
            .replace('{new}', newName)
      if (!window.confirm(msg)) return
      try {
        const data = await apiPost<{ updated?: Record<string, number>; merged?: boolean }>(
          '/admin/outfit-rules/rename',
          { old: oldName, new: newName },
        )
        const headline = data.merged ? t('Merged') : t('Renamed')
        toast(`${headline} — ${describeRenameUpdated(data.updated)}`)
        await reload()
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [rules, t, toast, reload],
  )

  const saveAll = useCallback(async () => {
    setStatus({ kind: 'saving', msg: t('Saving…') })
    try {
      await apiPut('/admin/outfit-rules/data', { outfit_types: rules })
      setStatus({ kind: 'ok', msg: t('Saved.') })
      toast(t('Saved'))
    } catch (e) {
      setStatus({ kind: 'error', msg: t('Error') + ': ' + (e as Error).message })
    }
  }, [rules, t, toast])

  if (loading) return <div className="ga-loading">{t('Loading…')}</div>

  return (
    <div className="ga-or-wrap">
      <div className="ga-or-toolbar">
        <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={saveAll}>
          {t('Save')}
        </button>
        <input
          className="ga-input ga-or-add-input"
          type="text"
          value={newType}
          onChange={(e) => setNewType(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') addType()
          }}
          placeholder={t("New outfit_type (e.g. 'streetwear')")}
        />
        <button className="ga-btn ga-btn-sm" onClick={addType}>
          + {t('Add')}
        </button>
        {status.kind !== 'idle' ? (
          <span
            className={
              'ga-or-status' +
              (status.kind === 'ok' ? ' is-ok' : '') +
              (status.kind === 'error' ? ' is-error' : '')
            }
          >
            {status.msg}
          </span>
        ) : (
          <span
            className="ga-or-status"
            title={t(
              'Defines which slots must be worn per outfit_type. Auto-fill on location change uses these rules. Per-character exceptions (set in the wardrobe) can override individual slots.',
            )}
          >
            ⓘ
          </span>
        )}
      </div>

      <div className="ga-or-table-wrap">
      <table className="ga-or-table">
        <thead>
          <tr>
            <th className="ga-or-type-col">outfit_type</th>
            <th title={t('Used when neither activity nor location specifies a type')}>{t('Def.')}</th>
            {slots.map((s) => (
              <th key={s}>{s}</th>
            ))}
            <th className="ga-or-actions-col" />
          </tr>
        </thead>
        <tbody>
          {sortedTypes.length === 0 ? (
            <tr>
              <td colSpan={slots.length + 3} style={{ textAlign: 'center', color: 'var(--text-muted, #8b949e)' }}>
                {t('No outfit_types yet')}
              </td>
            </tr>
          ) : (
            sortedTypes.map((typeName) => {
              const rule = rules[typeName] || {}
              const required = new Set(rule.required || [])
              const isDefault = !!rule.default
              return (
                <Row
                  key={typeName}
                  typeName={typeName}
                  rule={rule}
                  required={required}
                  isDefault={isDefault}
                  slots={slots}
                  onToggleSlot={toggleSlot}
                  onSetDefault={setDefault}
                  onSetDescription={setDescription}
                  onRename={renameType}
                  onDelete={deleteType}
                />
              )
            })
          )}
        </tbody>
      </table>
      </div>
    </div>
  )
}

interface RowProps {
  typeName: string
  rule: OutfitTypeRule
  required: Set<string>
  isDefault: boolean
  slots: string[]
  onToggleSlot: (typeName: string, slot: string, checked: boolean) => void
  onSetDefault: (typeName: string) => void
  onSetDescription: (typeName: string, value: string) => void
  onRename: (typeName: string) => void
  onDelete: (typeName: string) => void
}

function Row({
  typeName,
  rule,
  required,
  isDefault,
  slots,
  onToggleSlot,
  onSetDefault,
  onSetDescription,
  onRename,
  onDelete,
}: RowProps) {
  const { t } = useI18n()
  return (
    <tr>
      <td className="ga-or-type-col">
        <div className="ga-or-type-cell">
          <b>{typeName}</b>
          <AutoTextarea
            className="ga-or-desc-input"
            minRows={2}
            placeholder={t('Description for the LLM (e.g. club style: tight, neon)')}
            defaultValue={rule.description || ''}
            onBlur={(e) => onSetDescription(typeName, e.target.value)}
          />
        </div>
      </td>
      <td>
        <input
          type="radio"
          name="default-type"
          checked={isDefault}
          onChange={() => onSetDefault(typeName)}
          title={t('Mark as default')}
        />
      </td>
      {slots.map((s) => (
        <td key={s}>
          <input
            type="checkbox"
            checked={required.has(s)}
            onChange={(e) => onToggleSlot(typeName, s, e.target.checked)}
          />
        </td>
      ))}
      <td className="ga-or-actions-col">
        <button
          className="ga-btn ga-btn-sm"
          onClick={() => onRename(typeName)}
          title={t('Rename or merge into another type')}
        >
          {t('Rename')}
        </button>{' '}
        <button
          className="ga-btn ga-btn-sm ga-btn-danger"
          onClick={() => onDelete(typeName)}
          title={t('Delete')}
        >
          ×
        </button>
      </td>
    </tr>
  )
}
