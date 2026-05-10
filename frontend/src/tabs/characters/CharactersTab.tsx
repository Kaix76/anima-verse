import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiGet, apiPost } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'
import {
  loadActivities,
  loadCharacters,
  loadLocations,
  type ActivityRef,
  type CharacterRef,
  type LocationRef,
  type RoomRef,
} from '../../lib/refs'

/**
 * Game-Admin "Characters" tab — list-detail like Activities / Rules /
 * States / Items. Per-character live state (location, room, activity,
 * feeling) lives here. Template-switch and full profile editing stay
 * in their existing flows; this tab is for the lightweight overrides
 * an admin needs day-to-day.
 */

interface CurrentLocation {
  character: string
  current_location: string
  current_location_id: string
  current_activity: string
  current_room: string
  current_room_name: string
}

interface DraftPlacement {
  locationId: string
  roomId: string
  activity: string
  feeling: string
}

// Canonical moods — kept in sync with shared/config/moods.json. Updating
// the file requires updating this list, but moods rarely change so the
// duplication is acceptable; alternative would be a /moods endpoint.
const MOODS: Array<{ id: string; label: string }> = [
  { id: 'pleased', label: 'pleased' },
  { id: 'happy', label: 'happy' },
  { id: 'relaxed', label: 'relaxed' },
  { id: 'refreshed', label: 'refreshed' },
  { id: 'creative', label: 'creative' },
  { id: 'chatty', label: 'chatty' },
  { id: 'chatting', label: 'chatting' },
  { id: 'exuberant', label: 'exuberant' },
  { id: 'euphoric', label: 'euphoric' },
  { id: 'exhausted', label: 'exhausted' },
  { id: 'drunk', label: 'drunk' },
  { id: 'sweating', label: 'sweating' },
]

export function CharactersTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [characters, setCharacters] = useState<CharacterRef[]>([])
  const [locations, setLocations] = useState<LocationRef[]>([])
  const [activities, setActivities] = useState<ActivityRef[]>([])
  const [selected, setSelected] = useState<string>('')
  const [current, setCurrent] = useState<CurrentLocation | null>(null)
  const [currentFeeling, setCurrentFeeling] = useState<string>('')
  const [draft, setDraft] = useState<DraftPlacement | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    loadCharacters().then(setCharacters).catch(() => setCharacters([]))
    loadLocations().then(setLocations).catch(() => setLocations([]))
    loadActivities().then(setActivities).catch(() => setActivities([]))
  }, [])

  const reloadCurrent = useCallback(
    async (name: string) => {
      setCurrent(null)
      setCurrentFeeling('')
      setDraft(null)
      if (!name) return
      try {
        const [loc, feel] = await Promise.all([
          apiGet<CurrentLocation>(`/characters/${encodeURIComponent(name)}/current-location`),
          apiGet<{ current_feeling?: string }>(`/characters/${encodeURIComponent(name)}/current-feeling`),
        ])
        setCurrent(loc)
        setCurrentFeeling(feel.current_feeling || '')
        setDraft({
          locationId: loc.current_location_id || '',
          roomId: loc.current_room || '',
          activity: loc.current_activity || '',
          feeling: feel.current_feeling || '',
        })
      } catch (e) {
        toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
      }
    },
    [t, toast],
  )

  const onSelect = useCallback(
    (name: string) => {
      setSelected(name)
      reloadCurrent(name)
    },
    [reloadCurrent],
  )

  const selectedLocation: LocationRef | undefined = useMemo(
    () => locations.find((l) => l.id === draft?.locationId),
    [locations, draft],
  )

  const rooms: RoomRef[] = selectedLocation?.rooms || []

  // Group activities by their `_group` for the dropdown — same shape
  // the Library tab uses, makes the long list scannable.
  const activitiesByGroup = useMemo(() => {
    const groups = new Map<string, ActivityRef[]>()
    for (const a of activities) {
      const g = a._group || 'Other'
      if (!groups.has(g)) groups.set(g, [])
      groups.get(g)!.push(a)
    }
    for (const list of groups.values()) {
      list.sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id))
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b))
  }, [activities])

  const dirty = useMemo(() => {
    if (!current || !draft) return false
    const curLoc = current.current_location_id || ''
    const curRoom = current.current_room || ''
    const curAct = current.current_activity || ''
    const curFeel = currentFeeling || ''
    return (
      draft.locationId !== curLoc ||
      (draft.roomId || '') !== curRoom ||
      (draft.activity || '') !== curAct ||
      (draft.feeling || '') !== curFeel
    )
  }, [current, currentFeeling, draft])

  const save = useCallback(async () => {
    if (!selected || !draft || !current) return
    setSaving(true)
    try {
      const tasks: Promise<unknown>[] = []
      // Only POST what actually changed — fewer side-effects (e.g. the
      // location POST also runs the avatar-room-entry hook). Compare
      // against `current`, not against the pre-edit baseline that lives
      // in `draft` itself.
      const curLoc = current.current_location_id || ''
      const curRoom = current.current_room || ''
      const curAct = current.current_activity || ''
      if (draft.locationId !== curLoc || (draft.roomId || '') !== curRoom) {
        tasks.push(
          apiPost(`/characters/${encodeURIComponent(selected)}/current-location`, {
            current_location: draft.locationId,
            current_room: draft.roomId,
          }),
        )
      }
      if ((draft.activity || '') !== curAct) {
        tasks.push(
          apiPost(`/characters/${encodeURIComponent(selected)}/current-activity`, {
            current_activity: draft.activity,
          }),
        )
      }
      if ((draft.feeling || '') !== (currentFeeling || '')) {
        tasks.push(
          apiPost(`/characters/${encodeURIComponent(selected)}/current-feeling`, {
            current_feeling: draft.feeling,
          }),
        )
      }
      await Promise.all(tasks)
      toast(t('Saved'))
      await reloadCurrent(selected)
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    } finally {
      setSaving(false)
    }
  }, [current, currentFeeling, draft, reloadCurrent, selected, t, toast])

  const sortedCharacters = useMemo(
    () => [...characters].sort((a, b) => a.name.localeCompare(b.name)),
    [characters],
  )

  return (
    <div className="ga-twocol">
      <aside className="ga-twocol-left">
        <div className="ga-twocol-header">
          <h3>{t('Characters')}</h3>
        </div>
        <ul className="ga-list">
          {sortedCharacters.length === 0 ? (
            <li className="ga-list-empty">{t('No characters')}</li>
          ) : (
            sortedCharacters.map((c) => {
              const isActive = c.name === selected
              return (
                <li key={c.name}>
                  <button
                    type="button"
                    className={`ga-list-row${isActive ? ' is-active' : ''}`}
                    onClick={() => onSelect(c.name)}
                  >
                    <span className="ga-list-row-main">
                      <strong>{c.display_name || c.name}</strong>
                    </span>
                  </button>
                </li>
              )
            })
          )}
        </ul>
      </aside>
      <section className="ga-twocol-right">
        {!selected ? (
          <div className="ga-placeholder">{t('Pick a character to edit their settings.')}</div>
        ) : !current || !draft ? (
          <div className="ga-loading">{t('Loading…')}</div>
        ) : (
          <>
            <DetailToolbar
              title={selected}
              onSave={dirty ? save : undefined}
              onCancel={dirty ? () => reloadCurrent(selected) : undefined}
              cancelLabel={t('Revert')}
              disabled={saving}
            />
            <div className="ga-form">
              <div className="ga-form-section-label">{t('Placement')}</div>
              <div className="ga-form-row">
                <Field
                  label={t('Location')}
                  hint={
                    current.current_location
                      ? t('Currently at: {name}').replace('{name}', current.current_location)
                      : t('Currently nowhere — pick a location to place the character.')
                  }
                >
                  <select
                    className="ga-input"
                    value={draft.locationId}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        locationId: e.target.value,
                        // Reset room when location changes — the old
                        // room belongs to a different location.
                        roomId: '',
                      })
                    }
                  >
                    <option value="">— {t('nowhere')} —</option>
                    {locations.map((l) => (
                      <option key={l.id} value={l.id}>
                        {l.name || l.id}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label={t('Room')}
                  hint={
                    rooms.length === 0
                      ? t('Pick a location with rooms to choose a room.')
                      : current.current_room_name
                        ? t('Currently in: {name}').replace('{name}', current.current_room_name)
                        : t('Optional — leave empty for "anywhere in this location".')
                  }
                >
                  <select
                    className="ga-input"
                    value={draft.roomId}
                    onChange={(e) => setDraft({ ...draft, roomId: e.target.value })}
                    disabled={rooms.length === 0}
                  >
                    <option value="">— {t('any room')} —</option>
                    {rooms.map((r) => (
                      <option key={r.id} value={r.id || ''}>
                        {r.name || r.id}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              <div className="ga-form-section-label">{t('State')}</div>
              <div className="ga-form-row">
                <Field
                  label={t('Activity')}
                  hint={t(
                    'Setting an activity may also auto-move the character into a matching room (skipped while they are the active chat partner or the avatar).',
                  )}
                >
                  <select
                    className="ga-input"
                    value={draft.activity}
                    onChange={(e) => setDraft({ ...draft, activity: e.target.value })}
                  >
                    <option value="">— {t('none')} —</option>
                    {activitiesByGroup.map(([group, list]) => (
                      <optgroup key={group} label={group}>
                        {list.map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.name || a.id}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </Field>
                <Field
                  label={t('Mood')}
                  hint={t('Canonical mood id from shared/config/moods.json. Empty clears the mood.')}
                >
                  <select
                    className="ga-input"
                    value={draft.feeling}
                    onChange={(e) => setDraft({ ...draft, feeling: e.target.value })}
                  >
                    <option value="">— {t('none')} —</option>
                    {MOODS.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
