import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPost, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { loadActivities, loadCharacters, loadLocations, type ActivityRef, type CharacterRef, type LocationRef } from '../../lib/refs'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'
import { ListHeader } from '../../components/ListHeader'

type RuleType = 'block' | 'force' | 'discover'
type TargetScope = 'location' | 'any_room' | 'danger_level'

interface ForceAction {
  go_to?: 'stay' | 'home'
  activity_id?: string
}

interface RuleTarget {
  scope?: TargetScope
  location?: string
  rooms?: string[]
  action?: 'enter' | 'leave'
  min_danger_level?: number
}

interface Rule {
  id?: string
  name?: string
  type?: RuleType
  character?: string
  target?: RuleTarget
  force_action?: ForceAction
  discover?: { probability?: number }
  condition?: string
  message?: string
  _origin?: string
}

interface DraftRule {
  id: string
  name: string
  type: RuleType
  character: string
  target_storage: 'world' | 'shared'
  target_scope: TargetScope
  target_location: string
  target_rooms: string[]
  target_action: 'enter' | 'leave'
  target_min_danger: number
  force_go_to: 'stay' | 'home'
  force_activity: string
  discover_probability: number
  condition: string
  message: string
  origin: string
  isNew: boolean
}

const EMPTY_DRAFT: DraftRule = {
  id: '',
  name: '',
  type: 'block',
  character: '',
  target_storage: 'world',
  target_scope: 'location',
  target_location: '',
  target_rooms: [],
  target_action: 'enter',
  target_min_danger: 3,
  force_go_to: 'stay',
  force_activity: '',
  discover_probability: 0.05,
  condition: '',
  message: '',
  origin: '',
  isNew: true,
}

function ruleToDraft(r: Rule): DraftRule {
  const target = r.target || {}
  const force = r.force_action || {}
  return {
    id: r.id || '',
    name: r.name || '',
    type: r.type || 'block',
    character: r.character || '',
    target_storage: r._origin === 'shared' ? 'shared' : 'world',
    target_scope: target.scope || 'location',
    target_location: target.location || '',
    target_rooms: [...(target.rooms || [])],
    target_action: target.action || 'enter',
    target_min_danger: target.min_danger_level ?? 3,
    force_go_to: force.go_to || 'stay',
    force_activity: force.activity_id || '',
    discover_probability: r.discover?.probability ?? 0.05,
    condition: r.condition || '',
    message: r.message || '',
    origin: r._origin || 'world',
    isNew: false,
  }
}

function draftToRule(d: DraftRule): Rule {
  const r: Rule = {
    id: d.id || undefined,
    name: d.name.trim(),
    type: d.type,
    condition: d.condition.trim(),
    message: d.message.trim(),
  }
  if (d.character.trim()) r.character = d.character.trim()

  if (d.type === 'block') {
    const target: RuleTarget = { scope: d.target_scope, action: d.target_action }
    if (d.target_scope === 'location') {
      target.location = d.target_location
      if (d.target_rooms.length) target.rooms = d.target_rooms
    } else if (d.target_scope === 'danger_level') {
      target.min_danger_level = d.target_min_danger
    }
    r.target = target
  } else if (d.type === 'force') {
    r.force_action = { go_to: d.force_go_to, activity_id: d.force_activity || undefined }
  } else if (d.type === 'discover') {
    r.discover = { probability: d.discover_probability }
  }
  return r
}

export function RulesTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [rules, setRules] = useState<Rule[] | null>(null)
  const [draft, setDraft] = useState<DraftRule | null>(null)
  const [locations, setLocations] = useState<LocationRef[]>([])
  const [characters, setCharacters] = useState<CharacterRef[]>([])
  const [activities, setActivities] = useState<ActivityRef[]>([])

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ rules?: Rule[] }>('/rules')
      setRules(data.rules || [])
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    }
  }, [t, toast])

  useEffect(() => {
    reload()
  }, [reload])

  useEffect(() => {
    loadLocations().then(setLocations).catch(() => setLocations([]))
    loadCharacters().then(setCharacters).catch(() => setCharacters([]))
    loadActivities().then(setActivities).catch(() => setActivities([]))
  }, [])

  const sorted = useMemo(() => {
    if (!rules) return []
    return [...rules].sort((a, b) => (a.name || a.id || '').localeCompare(b.name || b.id || ''))
  }, [rules])

  const newRule = useCallback(() => {
    setDraft({ ...EMPTY_DRAFT })
  }, [])

  const editRule = useCallback((r: Rule) => {
    setDraft(ruleToDraft(r))
  }, [])

  const copyRule = useCallback(() => {
    setDraft((prev) =>
      prev ? { ...prev, id: '', name: `${prev.name} (copy)`.trim(), origin: '', isNew: true } : prev,
    )
  }, [])

  const update = useCallback(<K extends keyof DraftRule>(key: K, value: DraftRule[K]) => {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }, [])

  const save = useCallback(async () => {
    if (!draft) return
    if (!draft.name.trim() || !draft.type) {
      toast(t('Name and type are required'), 'error')
      return
    }
    const ruleBody = draftToRule(draft)
    const target = draft.target_storage
    try {
      if (draft.isNew || !draft.id) {
        await apiPost('/rules', { rule: ruleBody, target })
        toast(t('Rule created'))
      } else {
        await apiPut(`/rules/${encodeURIComponent(draft.id)}`, { rule: ruleBody, target })
        toast(t('Rule saved'))
      }
      await reload()
      setDraft(null)
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const deleteOverride = useCallback(async () => {
    if (!draft || draft.isNew) return
    if (!window.confirm(t('Remove world override of "{name}"?').replace('{name}', draft.name || draft.id))) return
    try {
      await apiDelete(`/rules/${encodeURIComponent(draft.id)}?target=world`)
      toast(t('World override removed'))
      await reload()
      setDraft(null)
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const deleteRule = useCallback(async () => {
    if (!draft || draft.isNew) return
    if (!window.confirm(t('Delete rule "{name}"?').replace('{name}', draft.name || draft.id))) return
    try {
      await apiDelete(`/rules/${encodeURIComponent(draft.id)}`)
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
      const oldTarget = draft.target_storage
      const ruleBody = draftToRule({ ...draft, target_storage: target })
      try {
        // Write to the new storage first…
        await apiPut(`/rules/${encodeURIComponent(draft.id)}`, { rule: ruleBody, target })
        // …then drop the old-storage entry so the move is exclusive. If the
        // old entry was a world override of a shared rule, deleting the
        // world copy lets the shared baseline take over (move-to-shared
        // case); going world→shared then deletes the world override; going
        // shared→world keeps shared as baseline + adds the world override.
        if (target === 'shared' && oldTarget !== 'shared') {
          await apiDelete(`/rules/${encodeURIComponent(draft.id)}?target=world`).catch(() => {})
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

  if (rules === null) return <div className="ga-loading">{t('Loading…')}</div>

  return (
    <div className="ga-twocol">
      <aside className="ga-twocol-left">
        <ListHeader
          title={t('Rules')}
          onNew={newRule}
          onCopy={copyRule}
          copyDisabled={!draft || draft.isNew}
        />
        <ul className="ga-list">
          {sorted.length === 0 ? (
            <li className="ga-list-empty">{t('No rules yet')}</li>
          ) : (
            sorted.map((r) => {
              const isActive = draft && !draft.isNew && draft.id === r.id
              return (
                <li key={r.id || r.name}>
                  <button
                    type="button"
                    className={`ga-list-row${isActive ? ' is-active' : ''}`}
                    onClick={() => editRule(r)}
                  >
                    <span className="ga-list-row-main">
                      <strong>{r.name || r.id}</strong>
                      <span className="ga-list-row-sub">— {r.type}</span>
                    </span>
                    <span className={`ga-source ga-source-${(r._origin || 'shared').replace(' ', '-')}`}>
                      {r._origin || 'shared'}
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
              title={draft.name || draft.id || t('New rule')}
              onSave={save}
              onCancel={() => setDraft(null)}
              onDelete={
                draft.isNew
                  ? undefined
                  : draft.origin === 'world override'
                    ? deleteOverride
                    : deleteRule
              }
              deleteLabel={
                draft.origin === 'world override' ? t('Remove override') : t('Delete')
              }
              onMove={draft.isNew ? undefined : move}
              storage={
                draft.origin === 'shared'
                  ? 'shared'
                  : draft.origin === 'world override'
                    ? 'world override'
                    : 'world'
              }
            />
            <RuleForm
              draft={draft}
              locations={locations}
              characters={characters}
              activities={activities}
              onUpdate={update}
            />
          </>
        ) : (
          <div className="ga-placeholder">{t('Click a rule or create a new one.')}</div>
        )}
      </section>
    </div>
  )
}

interface RuleFormProps {
  draft: DraftRule
  locations: LocationRef[]
  characters: CharacterRef[]
  activities: ActivityRef[]
  onUpdate: <K extends keyof DraftRule>(key: K, value: DraftRule[K]) => void
}

function RuleForm({ draft, locations, characters, activities, onUpdate }: RuleFormProps) {
  const { t } = useI18n()
  const selectedLocation = locations.find((l) => l.id === draft.target_location)
  const rooms = selectedLocation?.rooms || []

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

      <Field label={t('Name')}>
        <input
          className="ga-input"
          value={draft.name}
          placeholder={t('Rule name')}
          onChange={(e) => onUpdate('name', e.target.value)}
        />
      </Field>

      <div className="ga-form-row">
        <Field label={t('Type')}>
          <select
            className="ga-input"
            value={draft.type}
            onChange={(e) => onUpdate('type', e.target.value as RuleType)}
          >
            <option value="block">{t('Block')}</option>
            <option value="force">{t('Force')}</option>
            <option value="discover">{t('Discover')}</option>
          </select>
        </Field>
        <Field
          label={t('Character')}
          hint={t('Empty applies the rule to all characters.')}
        >
          <select
            className="ga-input"
            value={draft.character}
            onChange={(e) => onUpdate('character', e.target.value)}
          >
            <option value="">— {t('all characters')} —</option>
            {characters.map((c) => (
              <option key={c.name} value={c.name}>
                {c.display_name || c.name}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field
        label={t('Storage')}
        hint={t('Shared is global for all worlds. World is this world only and overrides shared by id.')}
      >
        <select
          className="ga-input"
          value={draft.target_storage}
          onChange={(e) => onUpdate('target_storage', e.target.value as 'world' | 'shared')}
        >
          <option value="world">{t('World-specific (this world only)')}</option>
          <option value="shared">{t('Shared (all worlds)')}</option>
        </select>
      </Field>

      {draft.type === 'block' ? (
        <div className="ga-form-row" style={{ alignItems: 'flex-start' }}>
          <Field label={t('Target')}>
            <select
              className="ga-input"
              value={draft.target_scope}
              onChange={(e) => onUpdate('target_scope', e.target.value as TargetScope)}
            >
              <option value="location">{t('Place / Room')}</option>
              <option value="any_room">{t('Any room (anywhere)')}</option>
              <option value="danger_level">{t('Danger level')}</option>
            </select>
          </Field>
          <Field label={t('Action')}>
            <select
              className="ga-input"
              value={draft.target_action}
              onChange={(e) => onUpdate('target_action', e.target.value as 'enter' | 'leave')}
            >
              <option value="enter">{t('Enter')}</option>
              <option value="leave">{t('Leave')}</option>
            </select>
          </Field>
        </div>
      ) : null}

      {draft.type === 'block' && draft.target_scope === 'location' ? (
        <>
          <Field label={t('Place')}>
            <select
              className="ga-input"
              value={draft.target_location}
              onChange={(e) => onUpdate('target_location', e.target.value)}
            >
              <option value="">-- {t('select')} --</option>
              {locations.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name || l.id}
                </option>
              ))}
            </select>
          </Field>
          {rooms.length > 0 ? (
            <Field label={t('Rooms')} hint={t('Multi-select with Ctrl/Cmd. No selection means the whole location.')}>
              <select
                className="ga-input"
                multiple
                size={6}
                value={draft.target_rooms}
                onChange={(e) => {
                  const next = Array.from(e.target.selectedOptions, (o) => o.value)
                  onUpdate('target_rooms', next)
                }}
              >
                {rooms.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name || r.id}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </>
      ) : null}

      {draft.type === 'block' && draft.target_scope === 'danger_level' ? (
        <Field label={t('Minimum danger level')}>
          <input
            type="number"
            className="ga-input"
            min={1}
            max={5}
            style={{ width: 80 }}
            value={draft.target_min_danger}
            onChange={(e) => onUpdate('target_min_danger', parseInt(e.target.value, 10) || 0)}
          />
        </Field>
      ) : null}

      {draft.type === 'force' ? (
        <>
          <Field label={t('Forced action')}>
            <select
              className="ga-input"
              value={draft.force_go_to}
              onChange={(e) => onUpdate('force_go_to', e.target.value as 'stay' | 'home')}
            >
              <option value="stay">{t('Stay (in place)')}</option>
              <option value="home">{t('Home (sleep place)')}</option>
            </select>
          </Field>
          <Field label={t('Activity')}>
            <select
              className="ga-input"
              value={draft.force_activity}
              onChange={(e) => onUpdate('force_activity', e.target.value)}
            >
              <option value="">-- {t('select')} --</option>
              {activities.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name || a.id}
                  {a._group ? ` (${a._group})` : ''}
                </option>
              ))}
            </select>
          </Field>
        </>
      ) : null}

      {draft.type === 'discover' ? (
        <Field
          label={t('Discovery probability per tick')}
          hint={t(
            'Rolled per agent-loop tick. On success a random adjacent unknown location is discovered. Skill checks go through the condition.',
          )}
        >
          <input
            type="number"
            className="ga-input"
            min={0}
            max={1}
            step={0.01}
            value={draft.discover_probability}
            onChange={(e) => onUpdate('discover_probability', parseFloat(e.target.value) || 0)}
          />
        </Field>
      ) : null}

      <Field
        label={t('Condition')}
        hint={
          <>
            <strong>{t('Status')}</strong> stamina&gt;N, courage&lt;N, stress&gt;N, lust&gt;N · {' '}
            <strong>{t('Time / presence')}</strong> alone, night, day · {' '}
            <strong>{t('Relationship')}</strong> relationship:Name&gt;N, romantic:Name&gt;N · {' '}
            <strong>{t('Mood')}</strong> mood:happy · {' '}
            <strong>{t('State')}</strong> condition:&lt;tag&gt; · {' '}
            <strong>{t('Activity')}</strong> current_activity:cooking · {' '}
            <strong>{t('Schedule')}</strong> schedule:sleeping, schedule:awake · {' '}
            <strong>{t('Inventory')}</strong> has_item:item_a1b2c3d4 · {' '}
            <strong>{t('Room')}</strong> room_has_item:holoprojector · {' '}
            <strong>{t('Combinators')}</strong> AND / OR / NOT
          </>
        }
      >
        <input
          className="ga-input"
          value={draft.condition}
          onChange={(e) => onUpdate('condition', e.target.value)}
          placeholder={t('e.g. has_item:item_a1b2c3d4 OR courage>50')}
        />
      </Field>

      <Field label={t('Message')}>
        <input
          className="ga-input"
          value={draft.message}
          onChange={(e) => onUpdate('message', e.target.value)}
          placeholder={t('e.g. You collapse from exhaustion')}
        />
      </Field>
    </div>
  )
}
