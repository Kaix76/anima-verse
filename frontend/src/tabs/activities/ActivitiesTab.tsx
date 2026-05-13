import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPost } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'
import { ListHeader } from '../../components/ListHeader'
import { EffectsEditor } from '../../components/EffectsEditor'
import { loadLocations, type LocationRef } from '../../lib/refs'

type Visibility = 'visible' | 'hidden' | 'disguised'
type EffectType = 'ongoing' | 'once'
type Category =
  | 'normal'
  | 'secret'
  | 'dangerous'
  | 'social'
  | 'creative'
  | 'investigation'
  | 'training'
  | 'rest'

interface FollowUp {
  activity_id?: string
  probability?: number
  condition?: string
}

/** Activity triggers — fire when the activity transitions through a phase.
 *  Backend dispatcher: app/core/activity_engine.py:execute_trigger.
 *  Each phase holds AT MOST ONE trigger (Backend expects a single dict per
 *  phase, not an array). The editor only exposes the trigger types most
 *  relevant for daily-routine modelling; unknown types from JSON are
 *  preserved verbatim and shown as read-only. */
type TriggerType =
  | ''
  | 'set_location'
  | 'set_activity'
  | 'mood_change'

interface Trigger {
  type: string
  // set_location: target = "home" | <location-id>; character_target = self/partner/avatar/<name>
  target?: string
  character_target?: string
  // set_activity: activity = <activity-id>; target = self/partner/avatar/<name>
  activity?: string
  // mood_change: mood = <mood-id>
  mood?: string
  // unknown fields are preserved as-is via _extra
  _extra?: Record<string, unknown>
}

type TriggerPhase = 'on_start' | 'on_complete' | 'on_discovered' | 'on_interrupted'
const TRIGGER_PHASES: TriggerPhase[] = ['on_start', 'on_complete', 'on_discovered', 'on_interrupted']

interface ActivityTriggers {
  on_start?: Trigger
  on_complete?: Trigger
  on_discovered?: Trigger
  on_interrupted?: Trigger
}

interface CumulativeEffect {
  threshold?: number
  condition_name?: string
  mood_influence?: string
  duration_hours?: number
  effects?: Record<string, number | string>
}

type EffectsValue = Record<string, number | string>

interface Activity {
  id: string
  name?: string
  description?: string
  _group?: string
  category?: Category
  visibility?: Visibility
  effects?: EffectsValue
  condition?: string
  effect_type?: EffectType
  cooldown_minutes?: number
  duration_minutes?: number
  outfit_type?: string[] | string
  interruptible?: boolean
  auto_pick?: boolean
  required_roles?: string
  requires_partner?: boolean
  partner_activity?: string
  fallback_activity?: string
  invitation_text?: string
  follow_up_activities?: FollowUp[]
  cumulative_effect?: CumulativeEffect
  triggers?: ActivityTriggers
  _shared?: boolean
  _origin?: string
}

interface DraftActivity {
  id: string
  name: string
  description: string
  group: string
  newGroup: string
  storage: 'world' | 'shared'
  category: Category
  visibility: Visibility
  effects_text: string
  condition: string
  effect_type: EffectType
  cooldown_minutes: number
  duration_minutes: number
  outfit_type: string[]
  interruptible: boolean
  auto_pick: boolean
  required_roles: string
  requires_partner: boolean
  partner_activity: string
  fallback_activity: string
  invitation_text: string
  follow_ups: FollowUp[]
  cum_threshold: string
  cum_condition_name: string
  cum_mood: string
  cum_duration: string
  cum_effects_text: string
  triggers: Record<TriggerPhase, Trigger>
  isNew: boolean
  origin: string
}

const CATEGORIES: Category[] = [
  'normal',
  'secret',
  'dangerous',
  'social',
  'creative',
  'investigation',
  'training',
  'rest',
]

const VISIBILITIES: Visibility[] = ['visible', 'hidden', 'disguised']

function effectsToText(eff?: EffectsValue): string {
  if (!eff) return ''
  return Object.entries(eff)
    .map(([k, v]) => `${k}: ${v}`)
    .join('\n')
}

function textToEffects(text: string): EffectsValue {
  const out: EffectsValue = {}
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$/)
    if (!m) continue
    const key = m[1]
    const raw = m[2]
    const num = Number(raw)
    out[key] = Number.isFinite(num) && /^[+-]?\d+(?:\.\d+)?$/.test(raw) ? num : raw
  }
  return out
}

function emptyTrigger(): Trigger {
  return { type: '' }
}

function emptyTriggers(): Record<TriggerPhase, Trigger> {
  return {
    on_start: emptyTrigger(),
    on_complete: emptyTrigger(),
    on_discovered: emptyTrigger(),
    on_interrupted: emptyTrigger(),
  }
}

const EMPTY_DRAFT: DraftActivity = {
  id: '',
  name: '',
  description: '',
  group: '',
  newGroup: '',
  storage: 'world',
  category: 'normal',
  visibility: 'visible',
  effects_text: '',
  condition: '',
  effect_type: 'ongoing',
  cooldown_minutes: 0,
  duration_minutes: 0,
  outfit_type: [],
  interruptible: true,
  auto_pick: true,
  required_roles: '',
  requires_partner: false,
  partner_activity: '',
  fallback_activity: '',
  invitation_text: '',
  follow_ups: [],
  cum_threshold: '',
  cum_condition_name: '',
  cum_mood: '',
  cum_duration: '',
  cum_effects_text: '',
  triggers: emptyTriggers(),
  isNew: true,
  origin: '',
}

/** Pull a trigger blob into editor shape — preserve unknown fields in _extra
 *  so JSON-only configs survive a UI save round-trip. */
function triggerToEditor(raw: unknown): Trigger {
  if (!raw || typeof raw !== 'object') return emptyTrigger()
  const obj = raw as Record<string, unknown>
  const known = new Set(['type', 'target', 'character_target', 'activity', 'mood'])
  const extra: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(obj)) {
    if (!known.has(k)) extra[k] = v
  }
  return {
    type: String(obj.type || ''),
    target: obj.target ? String(obj.target) : '',
    character_target: obj.character_target ? String(obj.character_target) : '',
    activity: obj.activity ? String(obj.activity) : '',
    mood: obj.mood ? String(obj.mood) : '',
    _extra: Object.keys(extra).length ? extra : undefined,
  }
}

/** Editor → JSON. Empty / cleared triggers are dropped entirely so the
 *  activity JSON stays minimal. Unknown fields (preserved in _extra) are
 *  merged back in. */
function triggerFromEditor(t: Trigger): Record<string, unknown> | null {
  if (!t.type) return null
  const out: Record<string, unknown> = { type: t.type, ...(t._extra || {}) }
  if (t.type === 'set_location') {
    if (!t.target) return null
    out.target = t.target
    if (t.character_target && t.character_target !== 'self') out.character_target = t.character_target
  } else if (t.type === 'set_activity') {
    if (!t.activity) return null
    out.activity = t.activity
    if (t.target && t.target !== 'self') out.target = t.target
  } else if (t.type === 'mood_change') {
    if (!t.mood) return null
    out.mood = t.mood
  }
  return out
}

function activityToDraft(a: Activity): DraftActivity {
  const cum = a.cumulative_effect || {}
  const outfitType = Array.isArray(a.outfit_type)
    ? a.outfit_type
    : a.outfit_type
      ? [a.outfit_type]
      : []
  const triggers = a.triggers || {}
  return {
    id: a.id,
    name: a.name || '',
    description: a.description || '',
    group: a._group || '',
    newGroup: '',
    storage: a._shared ? 'shared' : 'world',
    category: (a.category || 'normal') as Category,
    visibility: (a.visibility || 'visible') as Visibility,
    effects_text: effectsToText(a.effects),
    condition: a.condition || '',
    effect_type: (a.effect_type || 'ongoing') as EffectType,
    cooldown_minutes: a.cooldown_minutes || 0,
    duration_minutes: a.duration_minutes || 0,
    outfit_type: outfitType,
    interruptible: a.interruptible !== false,
    auto_pick: a.auto_pick !== false,
    required_roles: a.required_roles || '',
    requires_partner: !!a.requires_partner,
    partner_activity: a.partner_activity || '',
    fallback_activity: a.fallback_activity || '',
    invitation_text: a.invitation_text || '',
    follow_ups: [...(a.follow_up_activities || [])],
    cum_threshold: cum.threshold ? String(cum.threshold) : '',
    cum_condition_name: cum.condition_name || '',
    cum_mood: cum.mood_influence || '',
    cum_duration: cum.duration_hours ? String(cum.duration_hours) : '',
    cum_effects_text: effectsToText(cum.effects as EffectsValue),
    triggers: {
      on_start: triggerToEditor(triggers.on_start),
      on_complete: triggerToEditor(triggers.on_complete),
      on_discovered: triggerToEditor(triggers.on_discovered),
      on_interrupted: triggerToEditor(triggers.on_interrupted),
    },
    isNew: false,
    origin: a._origin || (a._shared ? 'shared' : 'world'),
  }
}

function draftToActivity(d: DraftActivity): Activity {
  const out: Activity = {
    id: d.id.trim() || d.name.trim().toLowerCase().replace(/\s+/g, '_'),
    name: d.name.trim(),
    description: d.description,
    category: d.category,
    visibility: d.visibility,
    effects: textToEffects(d.effects_text),
    condition: d.condition,
    effect_type: d.effect_type,
    cooldown_minutes: d.cooldown_minutes,
    duration_minutes: d.duration_minutes,
    interruptible: d.interruptible,
    auto_pick: d.auto_pick,
    required_roles: d.required_roles,
  }
  const group = (d.newGroup || d.group).trim()
  if (group) out._group = group
  if (d.outfit_type.length) out.outfit_type = d.outfit_type
  if (d.requires_partner) {
    out.requires_partner = true
    if (d.partner_activity) out.partner_activity = d.partner_activity
    if (d.fallback_activity) out.fallback_activity = d.fallback_activity
    if (d.invitation_text) out.invitation_text = d.invitation_text
  }
  if (d.follow_ups.length) {
    out.follow_up_activities = d.follow_ups.filter((f) => f.activity_id)
  }
  const threshold = parseInt(d.cum_threshold, 10)
  const duration = parseInt(d.cum_duration, 10)
  if (threshold && d.cum_condition_name) {
    out.cumulative_effect = {
      threshold,
      condition_name: d.cum_condition_name,
      mood_influence: d.cum_mood || undefined,
      duration_hours: duration || undefined,
      effects: textToEffects(d.cum_effects_text),
    }
  }
  const triggersOut: ActivityTriggers = {}
  for (const phase of TRIGGER_PHASES) {
    const blob = triggerFromEditor(d.triggers[phase])
    if (blob) triggersOut[phase] = blob as unknown as Trigger
  }
  if (Object.keys(triggersOut).length) out.triggers = triggersOut
  return out
}

export function ActivitiesTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [activities, setActivities] = useState<Activity[] | null>(null)
  const [groups, setGroups] = useState<Record<string, Activity[]>>({})
  const [draft, setDraft] = useState<DraftActivity | null>(null)
  const [search, setSearch] = useState('')
  const [outfitTypeOptions, setOutfitTypeOptions] = useState<string[]>([])
  const [stateOptions, setStateOptions] = useState<string[]>([])
  const [locations, setLocations] = useState<LocationRef[]>([])

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ activities?: Activity[]; groups?: Record<string, Activity[]> }>(
        '/activities/library',
      )
      setActivities(data.activities || [])
      setGroups(data.groups || {})
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    }
  }, [t, toast])

  useEffect(() => {
    reload()
    apiGet<{ outfit_types?: Record<string, unknown> }>('/admin/outfit-rules/data')
      .then((d) => setOutfitTypeOptions(Object.keys(d.outfit_types || {}).sort()))
      .catch(() => setOutfitTypeOptions([]))
    apiGet<{ filters?: Array<{ id: string }> }>('/admin/prompt-filters/data')
      .then((d) => setStateOptions((d.filters || []).map((f) => f.id).sort()))
      .catch(() => setStateOptions([]))
    loadLocations().then(setLocations).catch(() => setLocations([]))
  }, [reload])

  const knownGroups = useMemo(() => {
    const set = new Set(Object.keys(groups))
    if (draft?.group) set.add(draft.group)
    return Array.from(set).sort()
  }, [groups, draft])

  const filteredGroups = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return groups
    const out: Record<string, Activity[]> = {}
    for (const [g, list] of Object.entries(groups)) {
      const hit = list.filter((a) =>
        ((a.name || '').toLowerCase() + ' ' + a.id.toLowerCase()).includes(q),
      )
      if (hit.length) out[g] = hit
    }
    return out
  }, [groups, search])

  const newActivity = useCallback(() => {
    setDraft({ ...EMPTY_DRAFT })
  }, [])

  const editActivity = useCallback((a: Activity) => {
    setDraft(activityToDraft(a))
  }, [])

  const copyActivity = useCallback(() => {
    setDraft((prev) =>
      prev ? { ...prev, id: '', name: `${prev.name} (copy)`.trim(), origin: '', isNew: true } : prev,
    )
  }, [])

  const update = useCallback(<K extends keyof DraftActivity>(key: K, value: DraftActivity[K]) => {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }, [])

  const toggleOutfitType = useCallback((value: string) => {
    if (!value) return
    setDraft((prev) => {
      if (!prev) return prev
      const set = new Set(prev.outfit_type)
      if (set.has(value)) set.delete(value)
      else set.add(value)
      return { ...prev, outfit_type: Array.from(set) }
    })
  }, [])

  const updateFollowUp = useCallback((idx: number, patch: Partial<FollowUp>) => {
    setDraft((prev) => {
      if (!prev) return prev
      const next = [...prev.follow_ups]
      next[idx] = { ...next[idx], ...patch }
      return { ...prev, follow_ups: next }
    })
  }, [])

  const addFollowUp = useCallback(() => {
    setDraft((prev) =>
      prev ? { ...prev, follow_ups: [...prev.follow_ups, { activity_id: '', probability: 50 }] } : prev,
    )
  }, [])

  const removeFollowUp = useCallback((idx: number) => {
    setDraft((prev) => {
      if (!prev) return prev
      const next = [...prev.follow_ups]
      next.splice(idx, 1)
      return { ...prev, follow_ups: next }
    })
  }, [])

  const updateTrigger = useCallback(
    (phase: TriggerPhase, patch: Partial<Trigger>) => {
      setDraft((prev) => {
        if (!prev) return prev
        const next = { ...prev.triggers, [phase]: { ...prev.triggers[phase], ...patch } }
        return { ...prev, triggers: next }
      })
    },
    [],
  )

  const save = useCallback(async () => {
    if (!draft) return
    if (!draft.name.trim()) {
      toast(t('Name required'), 'error')
      return
    }
    try {
      const body = { activity: draftToActivity(draft), target: draft.storage }
      const r = await apiPost<{ activity?: Activity }>('/activities/library', body)
      toast(t('Activity saved'))
      await reload()
      // Keep the detail panel open on the just-saved activity. Server
      // returns the persisted record with its server-resolved id and
      // _shared/_group markers, so route the draft through activityToDraft
      // to pick those up.
      if (r.activity) {
        setDraft(activityToDraft({ ...r.activity, _shared: draft.storage === 'shared' }))
      }
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const remove = useCallback(async () => {
    if (!draft || draft.isNew) return
    if (!window.confirm(t('Delete activity "{name}"?').replace('{name}', draft.name || draft.id))) return
    try {
      await apiDelete(`/activities/library/${encodeURIComponent(draft.id)}?target=${draft.storage}`)
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
      const oldTarget = draft.storage
      const body = { activity: draftToActivity({ ...draft, storage: target }), target }
      try {
        await apiPost('/activities/library', body)
        if (oldTarget !== target) {
          await apiDelete(
            `/activities/library/${encodeURIComponent(draft.id)}?target=${oldTarget}`,
          ).catch(() => {})
        }
        toast(target === 'shared' ? t('Moved to shared') : t('Moved to world'))
        await reload()
        setDraft(null)
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [draft, reload, t, toast],
  )

  if (activities === null) return <div className="ga-loading">{t('Loading…')}</div>

  return (
    <div className="ga-twocol">
      <aside className="ga-twocol-left">
        <ListHeader
          title={t('Library')}
          onNew={newActivity}
          onCopy={copyActivity}
          copyDisabled={!draft || draft.isNew}
        />
        <input
          className="ga-input"
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('Search…')}
        />
        <ul className="ga-list" style={{ marginTop: 6 }}>
          {Object.keys(filteredGroups).length === 0 ? (
            <li className="ga-list-empty">{t('No activities')}</li>
          ) : (
            Object.entries(filteredGroups).map(([groupName, acts]) => (
              <li key={groupName} className="ga-act-group">
                <div className="ga-act-group-head">{groupName}</div>
                {acts.map((a) => {
                  const isActive = draft && !draft.isNew && draft.id === a.id
                  return (
                    <button
                      key={a.id}
                      type="button"
                      className={`ga-list-row${isActive ? ' is-active' : ''}`}
                      onClick={() => editActivity(a)}
                    >
                      <span className="ga-list-row-main">
                        <strong>{a.name || a.id}</strong>
                        <span className="ga-list-row-sub">— {a.id}</span>
                      </span>
                      {a._origin && a._origin !== 'shared' ? (
                        <span className={`ga-source ga-source-${a._origin.replace(' ', '-')}`}>
                          {a._origin}
                        </span>
                      ) : null}
                    </button>
                  )
                })}
              </li>
            ))
          )}
        </ul>
      </aside>
      <section className="ga-twocol-right">
        {draft ? (
          <>
            <DetailToolbar
              title={draft.name || draft.id || t('New activity')}
              onSave={save}
              onCancel={() => setDraft(null)}
              onDelete={draft.isNew ? undefined : remove}
              onMove={draft.isNew ? undefined : move}
              storage={draft.storage}
            />
            <ActivityForm
              draft={draft}
              knownGroups={knownGroups}
              outfitTypeOptions={outfitTypeOptions}
              stateOptions={stateOptions}
              allActivities={activities}
              locations={locations}
              onUpdate={update}
              onToggleOutfitType={toggleOutfitType}
              onUpdateFollowUp={updateFollowUp}
              onAddFollowUp={addFollowUp}
              onRemoveFollowUp={removeFollowUp}
              onUpdateTrigger={updateTrigger}
            />
          </>
        ) : (
          <div className="ga-placeholder">{t('Click an activity or create a new one.')}</div>
        )}
      </section>
    </div>
  )
}

interface FormProps {
  draft: DraftActivity
  knownGroups: string[]
  outfitTypeOptions: string[]
  stateOptions: string[]
  allActivities: Activity[]
  locations: LocationRef[]
  onUpdate: <K extends keyof DraftActivity>(key: K, value: DraftActivity[K]) => void
  onToggleOutfitType: (value: string) => void
  onUpdateFollowUp: (idx: number, patch: Partial<FollowUp>) => void
  onAddFollowUp: () => void
  onRemoveFollowUp: (idx: number) => void
  onUpdateTrigger: (phase: TriggerPhase, patch: Partial<Trigger>) => void
}

function ActivityForm({
  draft,
  knownGroups,
  outfitTypeOptions,
  stateOptions,
  allActivities,
  locations,
  onUpdate,
  onToggleOutfitType,
  onUpdateFollowUp,
  onAddFollowUp,
  onRemoveFollowUp,
  onUpdateTrigger,
}: FormProps) {
  const { t } = useI18n()
  const remainingOutfitTypes = outfitTypeOptions.filter((o) => !draft.outfit_type.includes(o))

  return (
    <div className="ga-form">
      {draft.origin ? (
        <div
          className={`ga-source ga-source-${draft.origin.replace(' ', '-')}`}
          style={{ alignSelf: 'flex-start' }}
        >
          {draft.origin}
        </div>
      ) : null}

      <div className="ga-form-row">
        <Field label={t('Storage')}>
          <select
            className="ga-input"
            value={draft.storage}
            onChange={(e) => onUpdate('storage', e.target.value as 'world' | 'shared')}
          >
            <option value="world">{t('World-specific')}</option>
            <option value="shared">{t('Shared (all worlds)')}</option>
          </select>
        </Field>
        <Field label={t('Group')}>
          <select
            className="ga-input"
            value={draft.group}
            onChange={(e) => {
              if (e.target.value === '__new__') {
                onUpdate('group', '__new__')
              } else {
                onUpdate('group', e.target.value)
                onUpdate('newGroup', '')
              }
            }}
          >
            <option value="">— {t('group')} —</option>
            {knownGroups.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
            <option value="__new__">+ {t('new group')}</option>
          </select>
        </Field>
        {draft.group === '__new__' ? (
          <Field label={t('New group name')}>
            <input
              className="ga-input"
              value={draft.newGroup}
              onChange={(e) => onUpdate('newGroup', e.target.value)}
            />
          </Field>
        ) : null}
      </div>

      <div className="ga-form-row">
        <Field label={t('ID')}>
          <input
            className="ga-input"
            value={draft.id}
            placeholder="cocktails_drinking"
            onChange={(e) => onUpdate('id', e.target.value)}
          />
        </Field>
        <Field label={t('Name')}>
          <input
            className="ga-input"
            value={draft.name}
            onChange={(e) => onUpdate('name', e.target.value)}
          />
        </Field>
      </div>

      <Field label={t('Description')}>
        <input
          className="ga-input"
          value={draft.description}
          onChange={(e) => onUpdate('description', e.target.value)}
        />
      </Field>

      <div className="ga-form-row">
        <Field label={t('Category')}>
          <select
            className="ga-input"
            value={draft.category}
            onChange={(e) => onUpdate('category', e.target.value as Category)}
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('Visibility')}>
          <select
            className="ga-input"
            value={draft.visibility}
            onChange={(e) => onUpdate('visibility', e.target.value as Visibility)}
          >
            {VISIBILITIES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label={t('Condition')}>
        <input
          className="ga-input"
          value={draft.condition}
          placeholder={t('e.g. NOT alone AND night')}
          onChange={(e) => onUpdate('condition', e.target.value)}
        />
      </Field>


      <Field
        label={t('Outfit type')}
        hint={t('Empty inherits the room or location dress code. Examples: sunbathing → pool, cardio → sport, sleeping → bed.')}
      >
        <div className="ga-tags-row">
          {draft.outfit_type.map((o) => (
            <button key={o} type="button" className="ga-tag-pill" onClick={() => onToggleOutfitType(o)}>
              {o} ×
            </button>
          ))}
          <select
            className="ga-input"
            style={{ width: 'auto', fontSize: 11, padding: '2px 6px' }}
            value=""
            onChange={(e) => {
              if (e.target.value) onToggleOutfitType(e.target.value)
            }}
          >
            <option value="">+ {t('select')}</option>
            {remainingOutfitTypes.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </div>
      </Field>

      <div className="ga-form-row">
        <div className="ga-form-col">
          <Field
            label={t('Effects')}
            hint={t('Click a stat or mood on the right to append it as a new line.')}
          >
            <EffectsEditor value={draft.effects_text} onChange={(v) => onUpdate('effects_text', v)} />
          </Field>
          <Field label={t('Interruptible')} inline compact>
            <input
              type="checkbox"
              checked={draft.interruptible}
              onChange={(e) => onUpdate('interruptible', e.target.checked)}
            />
          </Field>
          <Field label={t('Requires partner')} inline compact>
            <input
              type="checkbox"
              checked={draft.requires_partner}
              onChange={(e) => onUpdate('requires_partner', e.target.checked)}
            />
          </Field>
          <Field label={t('Partner activity')}>
            <select
              className="ga-input"
              value={draft.partner_activity}
              onChange={(e) => onUpdate('partner_activity', e.target.value)}
            >
              <option value="">— {t('none')} —</option>
              {allActivities.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name || a.id}
                </option>
              ))}
            </select>
          </Field>
          {draft.requires_partner ? (
            <>
              <Field
                label={t('Fallback activity')}
                hint={t('Used when the partner refuses or no one is around. Empty rejects the activity.')}
              >
                <select
                  className="ga-input"
                  value={draft.fallback_activity}
                  onChange={(e) => onUpdate('fallback_activity', e.target.value)}
                >
                  <option value="">— {t('none')} —</option>
                  {allActivities.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name || a.id}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t('Invitation text')} hint={t('Empty falls back to "Want to {name}?"')}>
                <input
                  className="ga-input"
                  value={draft.invitation_text}
                  onChange={(e) => onUpdate('invitation_text', e.target.value)}
                />
              </Field>
            </>
          ) : null}
        </div>
        <div className="ga-form-col">
          <Field
            label={t('Effect type')}
            hint={t('"once" applies a discrete one-shot effect; cooldown blocks the LLM from re-setting it.')}
          >
            <select
              className="ga-input"
              value={draft.effect_type}
              onChange={(e) => onUpdate('effect_type', e.target.value as EffectType)}
            >
              <option value="ongoing">{t('ongoing (per hour)')}</option>
              <option value="once">{t('once (one-shot)')}</option>
            </select>
          </Field>
          <Field label={t('Cooldown in minutes')}>
            <input
              type="number"
              className="ga-input"
              min={0}
              value={draft.cooldown_minutes}
              onChange={(e) => onUpdate('cooldown_minutes', parseInt(e.target.value, 10) || 0)}
            />
          </Field>
          <Field label={t('Duration in minutes')}>
            <input
              type="number"
              className="ga-input"
              min={0}
              value={draft.duration_minutes}
              onChange={(e) => onUpdate('duration_minutes', parseInt(e.target.value, 10) || 0)}
            />
          </Field>
          <Field label={t('Availability')}>
            <select
              className="ga-input"
              value={draft.auto_pick ? 'true' : 'false'}
              onChange={(e) => onUpdate('auto_pick', e.target.value === 'true')}
            >
              <option value="true">{t('Selectable')}</option>
              <option value="false">{t('Follow-up only')}</option>
            </select>
          </Field>
          <Field
            label={t('Required roles')}
            hint={t('Comma-separated. Empty allows everyone. Examples: photographer, model.')}
          >
            <input
              className="ga-input"
              value={draft.required_roles}
              onChange={(e) => onUpdate('required_roles', e.target.value)}
            />
          </Field>
        </div>
      </div>

      <div className="ga-section">
        <div className="ga-form-section-label">{t('Follow-up activities')}</div>
        <div className="ga-form-hint">
          {t('After execution, automatically start (probability %).')}
        </div>
        {draft.follow_ups.map((fu, idx) => (
          <div key={idx} className="ga-form-row" style={{ marginTop: 4 }}>
            <select
              className="ga-input"
              style={{ flex: 1 }}
              value={fu.activity_id || ''}
              onChange={(e) => onUpdateFollowUp(idx, { activity_id: e.target.value })}
            >
              <option value="">— {t('activity')} —</option>
              {allActivities.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name || a.id}
                </option>
              ))}
            </select>
            <input
              type="number"
              className="ga-input"
              style={{ width: 70 }}
              min={1}
              max={100}
              value={fu.probability ?? 50}
              onChange={(e) => onUpdateFollowUp(idx, { probability: parseInt(e.target.value, 10) || 0 })}
              title={t('Probability %')}
            />
            <button className="ga-btn ga-btn-sm ga-btn-danger" onClick={() => onRemoveFollowUp(idx)}>
              ×
            </button>
          </div>
        ))}
        <button className="ga-btn ga-btn-sm" onClick={onAddFollowUp} style={{ marginTop: 4 }}>
          + {t('Follow-up')}
        </button>
      </div>

      <div className="ga-section">
        <div className="ga-form-section-label">{t('Cumulative effect')}</div>
        <div className="ga-form-row">
          <div className="ga-form-col">
            <Field label={t('Cumulative effects')}>
              <EffectsEditor
                value={draft.cum_effects_text}
                onChange={(v) => onUpdate('cum_effects_text', v)}
              />
            </Field>
          </div>
          <div className="ga-form-col">
            <Field label={t('Threshold')}>
              <input
                type="number"
                className="ga-input"
                min={2}
                value={draft.cum_threshold}
                placeholder="X"
                onChange={(e) => onUpdate('cum_threshold', e.target.value)}
              />
            </Field>
            <Field label={t('Triggers state')}>
              <select
                className="ga-input"
                value={draft.cum_condition_name}
                onChange={(e) => onUpdate('cum_condition_name', e.target.value)}
              >
                <option value="">— {t('none')} —</option>
                {stateOptions.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t('Mood')}>
              <input
                className="ga-input"
                value={draft.cum_mood}
                placeholder={t('e.g. drunk')}
                onChange={(e) => onUpdate('cum_mood', e.target.value)}
              />
            </Field>
            <Field label={t('Duration in hours')}>
              <input
                type="number"
                className="ga-input"
                min={1}
                value={draft.cum_duration}
                placeholder="2"
                onChange={(e) => onUpdate('cum_duration', e.target.value)}
              />
            </Field>
          </div>
        </div>
      </div>

      <div className="ga-section">
        <div className="ga-form-section-label">{t('Triggers')}</div>
        <div className="ga-form-hint">
          {t(
            'Fired when the activity enters a phase. Use "Set location → Home" on the Sleeping activity so a sleeping character returns to their home (or vanishes off-world when home is set to "Sleeps off-world").',
          )}
        </div>
        {TRIGGER_PHASES.map((phase) => (
          <TriggerRow
            key={phase}
            phase={phase}
            trigger={draft.triggers[phase]}
            locations={locations}
            allActivities={allActivities}
            onUpdate={(patch) => onUpdateTrigger(phase, patch)}
          />
        ))}
      </div>
    </div>
  )
}

const PHASE_LABEL: Record<TriggerPhase, string> = {
  on_start: 'On start',
  on_complete: 'On complete',
  on_discovered: 'On discovered',
  on_interrupted: 'On interrupted',
}

interface TriggerRowProps {
  phase: TriggerPhase
  trigger: Trigger
  locations: LocationRef[]
  allActivities: Activity[]
  onUpdate: (patch: Partial<Trigger>) => void
}

function TriggerRow({ phase, trigger, locations, allActivities, onUpdate }: TriggerRowProps) {
  const { t } = useI18n()
  const supported = ['', 'set_location', 'set_activity', 'mood_change']
  const isUnknown = Boolean(trigger.type) && !supported.includes(trigger.type)
  return (
    <div className="ga-form-row" style={{ alignItems: 'flex-start', marginTop: 6 }}>
      <Field label={t(PHASE_LABEL[phase])}>
        <select
          className="ga-input"
          value={isUnknown ? '__unknown__' : trigger.type}
          disabled={isUnknown}
          onChange={(e) => {
            const v = e.target.value as TriggerType
            // Reset type-specific fields when switching type — avoids stale
            // values from another type leaking into the saved JSON.
            onUpdate({ type: v, target: '', character_target: '', activity: '', mood: '' })
          }}
        >
          <option value="">— {t('none')} —</option>
          <option value="set_location">{t('Set location')}</option>
          <option value="set_activity">{t('Set activity')}</option>
          <option value="mood_change">{t('Set mood')}</option>
          {isUnknown ? <option value="__unknown__">{trigger.type} ({t('JSON-only')})</option> : null}
        </select>
      </Field>
      {trigger.type === 'set_location' ? (
        <>
          <Field
            label={t('Target')}
            hint={t('"home" routes to the character\'s home_location (incl. "Sleeps off-world").')}
          >
            <select
              className="ga-input"
              value={trigger.target || ''}
              onChange={(e) => onUpdate({ target: e.target.value })}
            >
              <option value="">— {t('select')} —</option>
              <option value="home">home</option>
              {locations.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name || l.id}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('Applies to')}>
            <select
              className="ga-input"
              value={trigger.character_target || 'self'}
              onChange={(e) => onUpdate({ character_target: e.target.value })}
            >
              <option value="self">self</option>
              <option value="partner">partner</option>
              <option value="avatar">avatar</option>
            </select>
          </Field>
        </>
      ) : null}
      {trigger.type === 'set_activity' ? (
        <>
          <Field label={t('Activity')}>
            <select
              className="ga-input"
              value={trigger.activity || ''}
              onChange={(e) => onUpdate({ activity: e.target.value })}
            >
              <option value="">— {t('select')} —</option>
              {allActivities.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name || a.id}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t('Applies to')}>
            <select
              className="ga-input"
              value={trigger.target || 'self'}
              onChange={(e) => onUpdate({ target: e.target.value })}
            >
              <option value="self">self</option>
              <option value="partner">partner</option>
              <option value="avatar">avatar</option>
            </select>
          </Field>
        </>
      ) : null}
      {trigger.type === 'mood_change' ? (
        <Field label={t('Mood')}>
          <input
            className="ga-input"
            value={trigger.mood || ''}
            onChange={(e) => onUpdate({ mood: e.target.value })}
            placeholder="e.g. relaxed"
          />
        </Field>
      ) : null}
      {isUnknown ? (
        <Field label={t('Raw')}>
          <code style={{ fontSize: 11 }}>{JSON.stringify({ ...trigger, _extra: undefined })}</code>
        </Field>
      ) : null}
    </div>
  )
}
