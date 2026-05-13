import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPost } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'
import { ListHeader } from '../../components/ListHeader'
import { IconPicker } from '../../components/IconPicker'

interface PromptFilter {
  id: string
  label?: string
  icon?: string
  condition?: string
  drop_blocks?: string[]
  prompt_modifier?: string
  image_modifier?: string
  enabled?: boolean
  source?: string
}

interface FiltersData {
  filters: PromptFilter[]
  block_keys: string[]
  condition_hint?: string
}

interface DraftState extends Required<Omit<PromptFilter, 'source'>> {
  originalId: string
  source: string
  isNew: boolean
}

const EMPTY_DRAFT: DraftState = {
  id: '',
  label: '',
  icon: '',
  condition: '',
  drop_blocks: [],
  prompt_modifier: '',
  image_modifier: '',
  enabled: true,
  originalId: '',
  source: '',
  isNew: true,
}

function asDraft(f: PromptFilter): DraftState {
  return {
    id: f.id,
    label: f.label || '',
    icon: f.icon || '',
    condition: f.condition || '',
    drop_blocks: [...(f.drop_blocks || [])],
    prompt_modifier: f.prompt_modifier || '',
    image_modifier: f.image_modifier || '',
    enabled: f.enabled !== false,
    originalId: f.id,
    source: f.source || '',
    isNew: false,
  }
}

export function StatesTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [data, setData] = useState<FiltersData | null>(null)
  const [draft, setDraft] = useState<DraftState | null>(null)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const next = await apiGet<FiltersData>('/admin/prompt-filters/data')
      setData(next)
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }, [t, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const sortedFilters = useMemo(() => {
    if (!data) return []
    return [...data.filters].sort((a, b) => a.id.localeCompare(b.id))
  }, [data])

  const newFilter = useCallback(() => {
    setDraft({ ...EMPTY_DRAFT })
  }, [])

  const editFilter = useCallback((f: PromptFilter) => {
    setDraft(asDraft(f))
  }, [])

  const copyFilter = useCallback(() => {
    setDraft((prev) => (prev ? { ...prev, id: '', isNew: true, source: '', originalId: '' } : prev))
  }, [])

  const updateDraft = useCallback(<K extends keyof DraftState>(key: K, value: DraftState[K]) => {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }, [])

  const toggleBlock = useCallback((block: string) => {
    setDraft((prev) => {
      if (!prev) return prev
      const set = new Set(prev.drop_blocks)
      if (set.has(block)) set.delete(block)
      else set.add(block)
      return { ...prev, drop_blocks: Array.from(set) }
    })
  }, [])

  const save = useCallback(async () => {
    if (!draft) return
    const id = draft.id.trim()
    if (!id) {
      toast(t('id required'), 'error')
      return
    }
    try {
      await apiPost('/admin/prompt-filters/save', {
        id,
        label: draft.label.trim(),
        icon: draft.icon.trim(),
        condition: draft.condition.trim(),
        drop_blocks: draft.drop_blocks,
        prompt_modifier: draft.prompt_modifier,
        image_modifier: draft.image_modifier,
        enabled: draft.enabled,
      })
      toast(t('Saved'))
      // Re-fetch and re-pin the just-saved filter so the detail panel
      // stays open. The save endpoint only echoes {status,id}, so look
      // the persisted row up from the reloaded list.
      const next = await apiGet<FiltersData>('/admin/prompt-filters/data')
      setData(next)
      const saved = next.filters.find((f) => f.id === id)
      if (saved) setDraft(asDraft(saved))
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const remove = useCallback(async () => {
    if (!draft || draft.isNew) return
    if (!window.confirm(t('Delete state "{id}"?').replace('{id}', draft.originalId))) return
    try {
      await apiDelete(`/admin/prompt-filters/${encodeURIComponent(draft.originalId)}`)
      toast(t('Deleted'))
      await reload()
      setDraft(null)
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const move = useCallback(
    async (target: 'world' | 'shared') => {
      if (!draft || draft.isNew) return
      try {
        await apiPost(`/admin/prompt-filters/${encodeURIComponent(draft.originalId)}/move`, { target })
        toast(target === 'shared' ? t('Moved to shared') : t('Moved to world'))
        await reload()
        setDraft(null)
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [draft, reload, t, toast],
  )

  if (loading || !data) return <div className="ga-loading">{t('Loading…')}</div>

  return (
    <div className="ga-twocol">
      <aside className="ga-twocol-left">
        <ListHeader
          title={t('States')}
          onNew={newFilter}
          onCopy={copyFilter}
          copyDisabled={!draft || draft.isNew}
        />
        <p className="ga-sched-muted">
          {t(
            'On match, the selected blocks are removed from the thought prompt, the modifier appears under effects_block, the icon shows in the character header and the image modifier is appended to the image prompt.',
          )}
          <br />
          {t('Sources: shared baseline + world overlay (world overrides shared by id).')}
        </p>
        <ul className="ga-list">
          {sortedFilters.length === 0 ? (
            <li className="ga-list-empty">{t('No states yet')}</li>
          ) : (
            sortedFilters.map((f) => {
              const isActive = draft && !draft.isNew && draft.originalId === f.id
              return (
                <li key={f.id}>
                  <button
                    type="button"
                    className={`ga-list-row${isActive ? ' is-active' : ''}`}
                    onClick={() => editFilter(f)}
                  >
                    <span className="ga-list-row-main">
                      {f.icon ? <span style={{ marginRight: 6 }}>{f.icon}</span> : null}
                      <code>{f.id}</code>
                      {f.label ? <span className="ga-list-row-sub">— {f.label}</span> : null}
                    </span>
                    <span className={`ga-source ga-source-${(f.source || 'shared').replace(' ', '-')}`}>
                      {f.source || 'shared'}
                    </span>
                  </button>
                </li>
              )
            })
          )}
        </ul>
      </aside>
      <section className="ga-twocol-right">
        {draft ? (
          <>
            <DetailToolbar
              title={draft.id || t('New state')}
              onSave={save}
              onCancel={() => setDraft(null)}
              onDelete={draft.isNew ? undefined : remove}
              onMove={draft.isNew ? undefined : move}
              storage={
                draft.source === 'shared'
                  ? 'shared'
                  : draft.source === 'world override'
                    ? 'world override'
                    : 'world'
              }
            />
            <DraftForm
              draft={draft}
              blockKeys={data.block_keys || []}
              onUpdate={updateDraft}
              onToggleBlock={toggleBlock}
            />
          </>
        ) : (
          <div className="ga-placeholder">{t('Click a state or create a new one.')}</div>
        )}
      </section>
    </div>
  )
}

interface DraftFormProps {
  draft: DraftState
  blockKeys: string[]
  onUpdate: <K extends keyof DraftState>(key: K, value: DraftState[K]) => void
  onToggleBlock: (block: string) => void
}

function DraftForm({ draft, blockKeys, onUpdate, onToggleBlock }: DraftFormProps) {
  const { t } = useI18n()
  const selectedBlocks = useMemo(() => new Set(draft.drop_blocks), [draft.drop_blocks])

  return (
    <div className="ga-form">
      <Field label={t('Enabled')} inline compact>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => onUpdate('enabled', e.target.checked)}
        />
      </Field>

      <div className="ga-form-row">
        <Field label={t('Filter ID')} hint={t('a-z0-9_ — used as the profile tag that triggers the filter')}>
          <input
            className="ga-input"
            style={{ fontFamily: 'monospace' }}
            value={draft.id}
            placeholder="filter-id"
            onChange={(e) => onUpdate('id', e.target.value)}
          />
        </Field>
        <Field label={t('Label')}>
          <input
            className="ga-input"
            value={draft.label}
            placeholder={t("e.g. 'Drunk — clouded memory'")}
            onChange={(e) => onUpdate('label', e.target.value)}
          />
        </Field>
      </div>

      <div className="ga-form-row">
        <Field label={t('Icon')} compact>
          <div style={{ width: 110 }}>
            <IconPicker value={draft.icon} onChange={(v) => onUpdate('icon', v)} />
          </div>
        </Field>
        <Field
          label={t('Additional condition')}
          hint={t('Optional. Filter triggers via the id-tag; this expression triggers it additionally (e.g. stamina<10).')}
        >
          <input
            className="ga-input"
            value={draft.condition}
            placeholder="stamina<10"
            onChange={(e) => onUpdate('condition', e.target.value)}
          />
        </Field>
      </div>

      <Field label={t('Drop blocks')} hint={t('Click to toggle which prompt blocks are removed when this filter matches.')}>
        <div className="ga-block-picker">
          {blockKeys.length === 0 ? (
            <span className="ga-form-hint">{t('No block keys provided.')}</span>
          ) : (
            blockKeys.map((b) => {
              const on = selectedBlocks.has(b)
              return (
                <button
                  key={b}
                  type="button"
                  className={`ga-block${on ? ' is-on' : ''}`}
                  onClick={() => onToggleBlock(b)}
                >
                  {b}
                </button>
              )
            })
          )}
        </div>
      </Field>

      <Field
        label={t('Prompt modifier')}
        hint={t('Text appended to effects_block. Placeholder {avatar} is replaced with the active avatar name.')}
      >
        <textarea
          className="ga-textarea"
          rows={3}
          value={draft.prompt_modifier}
          placeholder="You are drunk. Recent conversations feel fuzzy …"
          onChange={(e) => onUpdate('prompt_modifier', e.target.value)}
        />
      </Field>

      <Field
        label={t('Image modifier')}
        hint={t("Visual hints for image generation, e.g. 'flushed cheeks, glassy eyes'.")}
      >
        <textarea
          className="ga-textarea"
          rows={2}
          value={draft.image_modifier}
          placeholder="flushed cheeks, glassy eyes"
          onChange={(e) => onUpdate('image_modifier', e.target.value)}
        />
      </Field>

    </div>
  )
}
