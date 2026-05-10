import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPost, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { loadCharacters, type CharacterRef } from '../../lib/refs'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'
import { ListHeader } from '../../components/ListHeader'
import { EffectsEditor } from '../../components/EffectsEditor'
import { Silhouette } from '../../components/Silhouette'

type Category =
  | 'outfit_piece'
  | 'key'
  | 'tool'
  | 'consumable'
  | 'evidence'
  | 'gift'
  | 'spell'
  | 'quest'
  | 'decoration'
type Rarity = 'common' | 'rare' | 'unique'

interface OutfitPieceMeta {
  outfit_types?: string[]
  slots?: string[]
  covers?: string[]
  partially_covers?: string[]
}

interface Item {
  id: string
  name?: string
  description?: string
  category?: Category
  rarity?: Rarity
  stackable?: boolean
  transferable?: boolean
  consumable?: boolean
  image_prompt?: string
  prompt_fragment?: string
  outfit_piece?: OutfitPieceMeta
  // Effects stored as a dict on the server (same as activities). The form
  // edits a "key: value" text representation; we (de)serialize on edit.
  effects?: Record<string, number | string> | string
  apply_condition?: string
  condition_duration?: number
  has_image?: boolean
  image?: string
  _shared?: boolean
  // Spell / magic / tracker / evidence fields. The spell-section editor
  // (rendered for category=spell) reads/writes them via `extras`; for
  // other categories they are passed through untouched on save.
  incantation?: string
  spell_mode?: string
  clone_item_id?: string
  success_chance?: number
  copy_on_give?: boolean
  success_text?: string
  fail_text?: string
  cast_activity?: string
  anchor_item_id?: string
  teleport_subject?: string
  tracks_character?: string
  reveals_secret?: string
}

interface Owner {
  character: string
  quantity: number
  equipped: boolean
}

interface DraftItem {
  id: string
  name: string
  description: string
  category: Category
  rarity: Rarity
  stackable: boolean
  transferable: boolean
  consumable: boolean
  image_prompt: string
  prompt_fragment: string
  outfit_types: string[]
  slots: string[]
  covers: string[]
  partially_covers: string[]
  effects: string
  apply_condition: string
  condition_duration: number
  // Round-trip storage for the magic / tracker / evidence fields. We
  // don't show editors for them yet but preserve whatever the server
  // returns so saving doesn't strip them.
  extras: Record<string, unknown>
  isNew: boolean
}

// apply_condition + condition_duration_hours live INSIDE the effects
// dict on the server (see app/models/inventory.py:1303). We pull them
// out so the form can render them as discrete fields, and re-embed on
// save. Without this round-trip neither value persists across reloads.
const CONDITION_KEYS = new Set(['apply_condition', 'condition_duration_hours'])

function effectsToText(eff: Item['effects']): string {
  if (!eff) return ''
  if (typeof eff === 'string') return eff
  return Object.entries(eff)
    .filter(([k]) => !CONDITION_KEYS.has(k))
    .map(([k, v]) => `${k}: ${v}`)
    .join('\n')
}

function textToEffects(text: string): Record<string, number | string> {
  const out: Record<string, number | string> = {}
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$/)
    if (!m) continue
    const raw = m[2]
    const num = Number(raw)
    out[m[1]] = Number.isFinite(num) && /^[+-]?\d+(?:\.\d+)?$/.test(raw) ? num : raw
  }
  return out
}

function extractConditionFromEffects(eff: Item['effects']): {
  apply_condition: string
  condition_duration: number
} {
  if (!eff || typeof eff === 'string') {
    return { apply_condition: '', condition_duration: 2 }
  }
  const cond = String(eff.apply_condition ?? '').trim()
  const durRaw = eff.condition_duration_hours
  const dur = Number(durRaw)
  return {
    apply_condition: cond,
    condition_duration: Number.isFinite(dur) && dur > 0 ? Math.floor(dur) : 2,
  }
}

const EXTRA_KEYS = [
  'incantation',
  'spell_mode',
  'clone_item_id',
  'success_chance',
  'copy_on_give',
  'success_text',
  'fail_text',
  'cast_activity',
  'anchor_item_id',
  'teleport_subject',
  'tracks_character',
  'reveals_secret',
] as const

const CATEGORIES: Category[] = [
  'outfit_piece',
  'key',
  'tool',
  'consumable',
  'evidence',
  'gift',
  'spell',
  'quest',
  'decoration',
]

const RARITIES: Rarity[] = ['common', 'rare', 'unique']

const VALID_PIECE_SLOTS = [
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

const EMPTY_DRAFT: DraftItem = {
  id: '',
  name: '',
  description: '',
  category: 'tool',
  rarity: 'common',
  stackable: false,
  transferable: true,
  consumable: false,
  image_prompt: '',
  prompt_fragment: '',
  outfit_types: [],
  slots: [],
  covers: [],
  partially_covers: [],
  effects: '',
  apply_condition: '',
  condition_duration: 2,
  extras: {},
  isNew: true,
}

function itemToDraft(it: Item): DraftItem {
  const op = it.outfit_piece || {}
  const extras: Record<string, unknown> = {}
  const itAny = it as unknown as Record<string, unknown>
  for (const k of EXTRA_KEYS) {
    const v = itAny[k]
    if (v !== undefined && v !== null && v !== '') extras[k] = v
  }
  return {
    id: it.id,
    name: it.name || '',
    description: it.description || '',
    category: (it.category || 'tool') as Category,
    rarity: (it.rarity || 'common') as Rarity,
    stackable: !!it.stackable,
    transferable: it.transferable !== false,
    consumable: !!it.consumable,
    image_prompt: it.image_prompt || '',
    prompt_fragment: it.prompt_fragment || '',
    outfit_types: [...(op.outfit_types || [])],
    slots: [...(op.slots || [])],
    covers: [...(op.covers || [])],
    partially_covers: [...(op.partially_covers || [])],
    effects: effectsToText(it.effects),
    ...extractConditionFromEffects(it.effects),
    extras,
    isNew: false,
  }
}

function draftToBody(d: DraftItem): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: d.name.trim(),
    description: d.description,
    category: d.category,
    rarity: d.rarity,
    stackable: d.stackable,
    transferable: d.transferable,
    consumable: d.consumable,
    image_prompt: d.image_prompt,
    prompt_fragment: d.prompt_fragment,
  }
  if (d.category === 'outfit_piece') {
    body.outfit_piece = {
      outfit_types: d.outfit_types,
      slots: d.slots,
      covers: d.covers,
      partially_covers: d.partially_covers,
    }
  }
  // "Effect on consume / cast" section: consumables AND spells write
  // effects. apply_condition + condition_duration_hours live INSIDE the
  // effects dict (server reads them from there in inventory.py:1303),
  // so we embed them rather than sending them at the top level.
  if (d.consumable || d.category === 'spell') {
    const eff = textToEffects(d.effects)
    if (d.apply_condition) {
      eff.apply_condition = d.apply_condition
      if (d.condition_duration > 0) {
        eff.condition_duration_hours = d.condition_duration
      }
    }
    body.effects = eff
  }
  // Pass-through magic / tracker / evidence fields so editing a spell
  // item doesn't strip its incantation / spell_mode / etc.
  for (const k of EXTRA_KEYS) {
    if (d.extras[k] !== undefined && d.extras[k] !== null && d.extras[k] !== '') {
      body[k] = d.extras[k]
    }
  }
  return body
}

export function ItemsTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [items, setItems] = useState<Item[] | null>(null)
  const [draft, setDraft] = useState<DraftItem | null>(null)
  const [search, setSearch] = useState('')
  const [filterCategory, setFilterCategory] = useState<Category | ''>('')
  const [filterRarity, setFilterRarity] = useState<Rarity | ''>('')
  const [characters, setCharacters] = useState<CharacterRef[]>([])
  const [owners, setOwners] = useState<Owner[]>([])
  const [outfitTypeOptions, setOutfitTypeOptions] = useState<string[]>([])
  const [conditionOptions, setConditionOptions] = useState<ConditionOption[]>([])

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ items?: Item[] }>('/inventory/items?include_shared=1')
      setItems(data.items || [])
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    }
  }, [t, toast])

  useEffect(() => {
    reload()
    loadCharacters().then(setCharacters).catch(() => setCharacters([]))
    apiGet<{ outfit_types?: Record<string, unknown> }>('/admin/outfit-rules/data')
      .then((d) => setOutfitTypeOptions(Object.keys(d.outfit_types || {}).sort()))
      .catch(() => setOutfitTypeOptions([]))
    apiGet<{ conditions?: ConditionOption[] }>('/world/conditions/list')
      .then((d) => setConditionOptions(d.conditions || []))
      .catch(() => setConditionOptions([]))
  }, [reload])

  const loadOwners = useCallback(async (id: string) => {
    if (!id) {
      setOwners([])
      return
    }
    try {
      const d = await apiGet<{ owners?: Owner[] }>(`/inventory/items/${encodeURIComponent(id)}/owners`)
      setOwners(d.owners || [])
    } catch {
      setOwners([])
    }
  }, [])

  const filtered = useMemo(() => {
    if (!items) return []
    const q = search.trim().toLowerCase()
    return items
      .filter((it) => {
        if (filterCategory && it.category !== filterCategory) return false
        if (filterRarity && it.rarity !== filterRarity) return false
        if (q && !((it.name || '').toLowerCase().includes(q) || (it.description || '').toLowerCase().includes(q))) {
          return false
        }
        return true
      })
      .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
  }, [items, search, filterCategory, filterRarity])

  const newItem = useCallback(() => {
    setDraft({ ...EMPTY_DRAFT })
    setOwners([])
  }, [])

  const editItem = useCallback(
    (it: Item) => {
      setDraft(itemToDraft(it))
      loadOwners(it.id)
    },
    [loadOwners],
  )

  const copyItem = useCallback(() => {
    setDraft((prev) =>
      prev ? { ...prev, id: '', name: `${prev.name} (copy)`.trim(), isNew: true } : prev,
    )
    setOwners([])
  }, [])

  const update = useCallback(<K extends keyof DraftItem>(key: K, value: DraftItem[K]) => {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }, [])

  // Schreibt einen Wert ins extras-Dict (Spell/Tracker/Evidence-Felder).
  // Leerwerte werden geloescht, damit draftToBody den Key nicht mit-postet.
  const updateExtra = useCallback((key: string, value: unknown) => {
    setDraft((prev) => {
      if (!prev) return prev
      const extras = { ...prev.extras }
      if (value === '' || value === null || value === undefined) delete extras[key]
      else extras[key] = value
      return { ...prev, extras }
    })
  }, [])

  const toggleListItem = useCallback(
    (key: 'outfit_types' | 'slots' | 'covers' | 'partially_covers', value: string) => {
      if (!value) return
      setDraft((prev) => {
        if (!prev) return prev
        const set = new Set(prev[key])
        if (set.has(value)) set.delete(value)
        else set.add(value)
        return { ...prev, [key]: Array.from(set) }
      })
    },
    [],
  )

  // Click-to-cycle on the silhouette: empty → slot → cover → partial → empty.
  // Each click moves the slot one step around the loop, so the user can
  // mark all four states without juggling the three tag pickers.
  const cycleSlot = useCallback((slot: string) => {
    setDraft((prev) => {
      if (!prev) return prev
      const inSlots = prev.slots.includes(slot)
      const inCovers = prev.covers.includes(slot)
      const inPartial = prev.partially_covers.includes(slot)
      const dropFrom = (list: string[]) => list.filter((s) => s !== slot)
      if (!inSlots && !inCovers && !inPartial) {
        return { ...prev, slots: [...prev.slots, slot] }
      }
      if (inSlots) {
        return { ...prev, slots: dropFrom(prev.slots), covers: [...prev.covers, slot] }
      }
      if (inCovers) {
        return { ...prev, covers: dropFrom(prev.covers), partially_covers: [...prev.partially_covers, slot] }
      }
      // inPartial → empty
      return { ...prev, partially_covers: dropFrom(prev.partially_covers) }
    })
  }, [])

  const save = useCallback(async () => {
    if (!draft) return
    if (!draft.name.trim()) {
      toast(t('Name required'), 'error')
      return
    }
    try {
      const body = draftToBody(draft)
      if (draft.isNew) {
        const r = await apiPost<{ item?: Item }>('/inventory/items', body)
        toast(t('Item created'))
        await reload()
        if (r.item) {
          setDraft(itemToDraft(r.item))
          loadOwners(r.item.id)
        }
      } else {
        const r = await apiPut<{ item?: Item }>(`/inventory/items/${encodeURIComponent(draft.id)}`, body)
        toast(t('Item saved'))
        await reload()
        if (r.item) setDraft(itemToDraft(r.item))
      }
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, loadOwners, reload, t, toast])

  const remove = useCallback(async () => {
    if (!draft || draft.isNew) return
    if (!window.confirm(t('Delete item "{name}"?').replace('{name}', draft.name || draft.id))) return
    try {
      await apiDelete(`/inventory/items/${encodeURIComponent(draft.id)}`)
      toast(t('Deleted'))
      await reload()
      setDraft(null)
      setOwners([])
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const move = useCallback(
    async (target: 'world' | 'shared') => {
      if (!draft || draft.isNew) return
      const path =
        target === 'shared'
          ? `/inventory/items/${encodeURIComponent(draft.id)}/move-to-shared`
          : `/inventory/items/${encodeURIComponent(draft.id)}/move-to-world`
      try {
        await apiPost(path, {})
        toast(target === 'shared' ? t('Moved to shared') : t('Moved to world'))
        await reload()
        setDraft(null)
        setOwners([])
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [draft, reload, t, toast],
  )

  const generateImage = useCallback(async () => {
    if (!draft || draft.isNew) return
    try {
      toast(t('Generating image…'))
      await apiPost(`/inventory/items/${encodeURIComponent(draft.id)}/generate-image`, {})
      toast(t('Image generation queued'))
      // Refresh after a brief delay so the new image URL has a chance to land.
      window.setTimeout(reload, 1500)
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [draft, reload, t, toast])

  const addOwner = useCallback(
    async (characterName: string, qty: number) => {
      if (!draft || draft.isNew || !characterName) return
      try {
        await apiPost(`/inventory/characters/${encodeURIComponent(characterName)}`, {
          item_id: draft.id,
          quantity: qty,
          obtained_method: 'manual',
        })
        toast(t('Given'))
        loadOwners(draft.id)
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [draft, loadOwners, t, toast],
  )

  const removeOwner = useCallback(
    async (characterName: string) => {
      if (!draft || draft.isNew) return
      try {
        await apiDelete(`/inventory/characters/${encodeURIComponent(characterName)}/${encodeURIComponent(draft.id)}`)
        toast(t('Removed'))
        loadOwners(draft.id)
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [draft, loadOwners, t, toast],
  )

  if (items === null) return <div className="ga-loading">{t('Loading…')}</div>

  const isOutfit = draft?.category === 'outfit_piece'

  return (
    <div className={`ga-items-grid${isOutfit ? ' has-silhouette' : ''}`}>
      <aside className="ga-items-list-col">
        <ListHeader
          title={t('Item library')}
          onNew={newItem}
          onCopy={copyItem}
          copyDisabled={!draft || draft.isNew}
        />
        <input
          className="ga-input"
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('Search…')}
        />
        <div className="ga-form-row" style={{ marginTop: 4 }}>
          <select
            className="ga-input"
            style={{ flex: 1 }}
            value={filterCategory}
            onChange={(e) => setFilterCategory(e.target.value as Category | '')}
          >
            <option value="">{t('All categories')}</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select
            className="ga-input"
            style={{ flex: 1 }}
            value={filterRarity}
            onChange={(e) => setFilterRarity(e.target.value as Rarity | '')}
          >
            <option value="">{t('All rarities')}</option>
            {RARITIES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <ul className="ga-list" style={{ marginTop: 6 }}>
          {filtered.length === 0 ? (
            <li className="ga-list-empty">{t('No items')}</li>
          ) : (
            filtered.map((it) => {
              const isActive = draft && !draft.isNew && draft.id === it.id
              return (
                <li key={it.id}>
                  <button
                    type="button"
                    className={`ga-list-row ga-cat-${it.category || 'tool'}${isActive ? ' is-active' : ''}`}
                    onClick={() => editItem(it)}
                  >
                    <span className="ga-list-row-main">
                      <strong>{it.name || it.id}</strong>
                      <span className="ga-list-row-sub">— {it.category || 'tool'}</span>
                    </span>
                    <span className="ga-source ga-source-shared" style={{ visibility: it._shared ? 'visible' : 'hidden' }}>
                      shared
                    </span>
                  </button>
                </li>
              )
            })
          )}
        </ul>
      </aside>

      {draft ? (
        <div className="ga-items-toolbar-row">
          <DetailToolbar
            title={draft.name || draft.id || t('New item')}
            onSave={save}
            onCancel={() => {
              setDraft(null)
              setOwners([])
            }}
            onDelete={draft.isNew ? undefined : remove}
            onMove={draft.isNew ? undefined : move}
            storage={
              items?.find((it) => it.id === draft.id)?._shared ? 'shared' : 'world'
            }
          />
        </div>
      ) : null}

      <section className="ga-items-form-col">
        {draft ? (
          <ItemForm
            draft={draft}
            items={items || []}
            outfitTypeOptions={outfitTypeOptions}
            conditionOptions={conditionOptions}
            onUpdate={update}
            onUpdateExtra={updateExtra}
            onToggleListItem={toggleListItem}
          />
        ) : (
          <div className="ga-placeholder">{t('Click an item or create a new one.')}</div>
        )}
      </section>

      {isOutfit && draft ? (
        <aside className="ga-items-silhouette-col">
          <Silhouette
            slots={draft.slots}
            covers={draft.covers}
            partially_covers={draft.partially_covers}
            onCycleSlot={cycleSlot}
          />
        </aside>
      ) : null}

      <aside className="ga-items-side-col">
        {draft && !draft.isNew ? (
          <>
            <div className="ga-items-image-panel">
              <div className="ga-form-section-label">{t('Image')}</div>
              <div className="ga-items-image-preview">
                <img src={`/inventory/items/${encodeURIComponent(draft.id)}/image?t=${Date.now()}`} alt="" />
              </div>
              <button className="ga-btn ga-btn-sm" onClick={generateImage}>
                {t('Generate image')}
              </button>
            </div>
            <div className="ga-items-owners-panel">
              <div className="ga-form-section-label">{t('Owners of this item')}</div>
              <OwnerAdder characters={characters} onAdd={addOwner} />
              {owners.length === 0 ? (
                <div className="ga-form-hint">{t('Nobody')}</div>
              ) : (
                <ul className="ga-items-owners-list">
                  {owners.map((o) => (
                    <li key={o.character}>
                      <span>
                        {o.character} <span className="ga-form-hint">×{o.quantity}</span>
                        {o.equipped ? <span className="ga-form-hint"> · {t('equipped')}</span> : null}
                      </span>
                      <button
                        className="ga-btn ga-btn-sm ga-btn-danger"
                        onClick={() => removeOwner(o.character)}
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        ) : (
          <div className="ga-placeholder">{t('Select an item to manage image and owners.')}</div>
        )}
      </aside>
    </div>
  )
}

interface ConditionOption {
  name: string
  label?: string
  icon?: string
}

interface ItemFormProps {
  draft: DraftItem
  items: Item[]
  outfitTypeOptions: string[]
  conditionOptions: ConditionOption[]
  onUpdate: <K extends keyof DraftItem>(key: K, value: DraftItem[K]) => void
  onUpdateExtra: (key: string, value: unknown) => void
  onToggleListItem: (key: 'outfit_types' | 'slots' | 'covers' | 'partially_covers', value: string) => void
}

function ItemForm({ draft, items, outfitTypeOptions, conditionOptions, onUpdate, onUpdateExtra, onToggleListItem }: ItemFormProps) {
  const { t } = useI18n()
  const isOutfit = draft.category === 'outfit_piece'
  const isSpell = draft.category === 'spell'
  return (
    <div className="ga-form">
      <div className="ga-form-row">
        <Field label={t('Name')} hint={t('English. Also used as display name.')}>
          <input
            className="ga-input"
            value={draft.name}
            onChange={(e) => onUpdate('name', e.target.value)}
          />
        </Field>
        <Field label={t('Description')}>
          <input
            className="ga-input"
            value={draft.description}
            onChange={(e) => onUpdate('description', e.target.value)}
          />
        </Field>
      </div>

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
        <Field label={t('Rarity')}>
          <select
            className="ga-input"
            value={draft.rarity}
            onChange={(e) => onUpdate('rarity', e.target.value as Rarity)}
          >
            {RARITIES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label={t('Flags')}>
        <div className="ga-form-row" style={{ gap: 14 }}>
          <label className="ga-form-check">
            <input type="checkbox" checked={draft.stackable} onChange={(e) => onUpdate('stackable', e.target.checked)} />
            {t('Stackable')}
          </label>
          <label className="ga-form-check">
            <input
              type="checkbox"
              checked={draft.transferable}
              onChange={(e) => onUpdate('transferable', e.target.checked)}
            />
            {t('Transferable')}
          </label>
          <label className="ga-form-check">
            <input
              type="checkbox"
              checked={draft.consumable}
              onChange={(e) => onUpdate('consumable', e.target.checked)}
            />
            {t('Consumable')}
          </label>
        </div>
      </Field>

      <Field label={t('Image prompt')} hint={t('Used to generate the item image.')}>
        <input
          className="ga-input"
          value={draft.image_prompt}
          placeholder={t("e.g. 'silver house key on wooden table, realistic'")}
          onChange={(e) => onUpdate('image_prompt', e.target.value)}
        />
      </Field>
      <Field label={t('Prompt fragment')} hint={t('Used in the character image when this item is held or worn.')}>
        <input
          className="ga-input"
          value={draft.prompt_fragment}
          placeholder={t("e.g. 'holding a hammer' or 'black leather jacket, slim fit'")}
          onChange={(e) => onUpdate('prompt_fragment', e.target.value)}
        />
      </Field>

      {isOutfit ? (
        <div className="ga-section">
          <div className="ga-form-section-label">{t('Outfit piece')}</div>
          <Field label={t('Outfit types')}>
            <TagPicker
              options={outfitTypeOptions}
              values={draft.outfit_types}
              onToggle={(v) => onToggleListItem('outfit_types', v)}
              allowFreeform
            />
          </Field>
          <Field label={t('Slots')} hint={t('Single-slot items wear one slot. Multi-slot items (dress, jumpsuit, thigh-highs) wear several.')}>
            <TagPicker
              options={VALID_PIECE_SLOTS}
              values={draft.slots}
              onToggle={(v) => onToggleListItem('slots', v)}
            />
          </Field>
          <Field label={t('Fully covers')}>
            <TagPicker
              options={VALID_PIECE_SLOTS}
              values={draft.covers}
              onToggle={(v) => onToggleListItem('covers', v)}
            />
          </Field>
          <Field label={t('Partially covers')}>
            <TagPicker
              options={VALID_PIECE_SLOTS}
              values={draft.partially_covers}
              onToggle={(v) => onToggleListItem('partially_covers', v)}
            />
          </Field>
        </div>
      ) : null}

      {isSpell ? (
        <div className="ga-section">
          <div className="ga-form-section-label">{t('Spell')}</div>
          <Field
            label={t('Incantation')}
            hint={t('Trigger phrase the avatar must say in chat. Detected case-insensitively.')}
          >
            <input
              className="ga-input"
              value={(draft.extras.incantation as string) || ''}
              placeholder={t("e.g. 'Heimfaden, zieh mich heim'")}
              onChange={(e) => onUpdateExtra('incantation', e.target.value)}
            />
          </Field>
          <div className="ga-form-row">
            <Field label={t('Mode')} hint={t('force = spell on target. gift = scroll/potion handed over.')}>
              <select
                className="ga-input"
                value={(draft.extras.spell_mode as string) || 'force'}
                onChange={(e) => onUpdateExtra('spell_mode', e.target.value)}
              >
                <option value="force">force</option>
                <option value="gift">gift</option>
              </select>
            </Field>
            <Field label={t('Success chance')} hint={t('0–100. Roll above = fail.')}>
              <input
                type="number"
                className="ga-input"
                style={{ width: 90 }}
                min={0}
                max={100}
                value={(draft.extras.success_chance as number) ?? 100}
                onChange={(e) => onUpdateExtra('success_chance', parseInt(e.target.value, 10) || 0)}
              />
            </Field>
            <Field label={t('Caster keeps spell')} hint={t('On = learned spell (reusable). Off = scroll/potion (consumed).')}>
              <label className="ga-form-check" style={{ marginTop: 6 }}>
                <input
                  type="checkbox"
                  checked={!!draft.extras.copy_on_give}
                  onChange={(e) => onUpdateExtra('copy_on_give', e.target.checked)}
                />
                {t('copy_on_give')}
              </label>
            </Field>
          </div>
          <div className="ga-form-row">
            <Field label={t('Success text')} hint={t('Hint injected into the target NPC prompt on success.')}>
              <textarea
                className="ga-textarea"
                rows={2}
                value={(draft.extras.success_text as string) || ''}
                onChange={(e) => onUpdateExtra('success_text', e.target.value)}
              />
            </Field>
            <Field label={t('Fail text')} hint={t('Hint injected on failure.')}>
              <textarea
                className="ga-textarea"
                rows={2}
                value={(draft.extras.fail_text as string) || ''}
                onChange={(e) => onUpdateExtra('fail_text', e.target.value)}
              />
            </Field>
          </div>
          <div className="ga-form-row">
            <Field label={t('Cast activity')} hint={t('Optional library activity set on the caster after the cast (cooldown).')}>
              <input
                className="ga-input"
                value={(draft.extras.cast_activity as string) || ''}
                placeholder={t("e.g. 'channeling'")}
                onChange={(e) => onUpdateExtra('cast_activity', e.target.value)}
              />
            </Field>
            <Field label={t('Effect item (clone_item_id)')} hint={t('Item handed to the target on success. Defaults to the spell item itself.')}>
              <select
                className="ga-input"
                value={(draft.extras.clone_item_id as string) || ''}
                onChange={(e) => onUpdateExtra('clone_item_id', e.target.value)}
              >
                <option value="">{t('-- spell item itself --')}</option>
                {items
                  .filter((it) => it.id !== draft.id)
                  .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
                  .map((it) => (
                    <option key={it.id} value={it.id}>
                      {it.name || it.id}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
          <div className="ga-form-section-label" style={{ marginTop: 12 }}>{t('Anchor teleport')}</div>
          <div className="ga-form-row">
            <Field
              label={t('Anchor item')}
              hint={t('When set, casting teleports to wherever this item currently is (a character carrying it, or a room it lies in). Leave empty for non-teleport spells.')}
            >
              <select
                className="ga-input"
                value={(draft.extras.anchor_item_id as string) || ''}
                onChange={(e) => onUpdateExtra('anchor_item_id', e.target.value)}
              >
                <option value="">{t('-- no anchor (not a teleport) --')}</option>
                {items
                  .filter((it) => it.id !== draft.id)
                  .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
                  .map((it) => (
                    <option key={it.id} value={it.id}>
                      {it.name || it.id} <span>— {it.category || 'tool'}</span>
                    </option>
                  ))}
              </select>
            </Field>
            <Field
              label={t('Direction')}
              hint={t('caster: caster jumps to the anchor. anchor_holder: anchor carrier is pulled to the caster (only works if a character carries the anchor).')}
            >
              <select
                className="ga-input"
                value={(draft.extras.teleport_subject as string) || 'caster'}
                onChange={(e) => onUpdateExtra('teleport_subject', e.target.value)}
                disabled={!draft.extras.anchor_item_id}
              >
                <option value="caster">{t('caster → anchor')}</option>
                <option value="anchor_holder">{t('anchor holder → caster')}</option>
              </select>
            </Field>
          </div>
        </div>
      ) : null}

      {draft.consumable || draft.category === 'spell' ? (
        <div className="ga-section">
          <div className="ga-form-section-label">
            {draft.category === 'spell' ? t('Effect on cast') : t('Effect on consume')}
          </div>
          <Field
            label={t('Effects')}
            hint={t('Format: "stat_change: +/-value" per line. Click a stat or mood to insert it.')}
          >
            <EffectsEditor value={draft.effects} onChange={(v) => onUpdate('effects', v)} />
          </Field>
          <div className="ga-form-row">
            <Field label={t('Apply condition')} hint={t('Optional. Activates a state tag in the character profile.')}>
              <select
                className="ga-input"
                value={draft.apply_condition}
                onChange={(e) => onUpdate('apply_condition', e.target.value)}
              >
                <option value="">{t('-- none --')}</option>
                {conditionOptions.map((c) => {
                  const icon = c.icon ? `${c.icon} ` : ''
                  const label = c.label ? ` — ${c.label}` : ''
                  return (
                    <option key={c.name} value={c.name}>
                      {icon}
                      {c.name}
                      {label}
                    </option>
                  )
                })}
                {draft.apply_condition &&
                !conditionOptions.some((c) => c.name === draft.apply_condition) ? (
                  <option value={draft.apply_condition}>
                    {draft.apply_condition} {t('(not in conditions)')}
                  </option>
                ) : null}
              </select>
            </Field>
            <Field label={t('Duration in hours')}>
              <input
                type="number"
                className="ga-input"
                style={{ width: 90 }}
                min={1}
                value={draft.condition_duration}
                onChange={(e) => onUpdate('condition_duration', parseInt(e.target.value, 10) || 0)}
              />
            </Field>
          </div>
        </div>
      ) : null}
    </div>
  )
}

interface TagPickerProps {
  options: string[]
  values: string[]
  onToggle: (value: string) => void
  allowFreeform?: boolean
}

function TagPicker({ options, values, onToggle, allowFreeform }: TagPickerProps) {
  const { t } = useI18n()
  const [draft, setDraft] = useState('')
  const remaining = options.filter((o) => !values.includes(o))
  return (
    <div className="ga-tags-row">
      {values.map((v) => (
        <button key={v} type="button" className="ga-tag-pill" onClick={() => onToggle(v)} title={t('Remove')}>
          {v} ×
        </button>
      ))}
      <select
        className="ga-input"
        style={{ width: 'auto', fontSize: 11, padding: '2px 6px' }}
        value=""
        onChange={(e) => {
          if (e.target.value) onToggle(e.target.value)
        }}
      >
        <option value="">+ {t('add')}</option>
        {remaining.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
      {allowFreeform ? (
        <input
          className="ga-input"
          style={{ width: 130, fontSize: 11, padding: '2px 6px' }}
          value={draft}
          placeholder={t('+ new')}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && draft.trim()) {
              onToggle(draft.trim().toLowerCase())
              setDraft('')
            }
          }}
        />
      ) : null}
    </div>
  )
}

interface OwnerAdderProps {
  characters: CharacterRef[]
  onAdd: (name: string, qty: number) => void
}

function OwnerAdder({ characters, onAdd }: OwnerAdderProps) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [qty, setQty] = useState(1)
  return (
    <div className="ga-form-row" style={{ marginTop: 6 }}>
      <select className="ga-input" style={{ flex: 1, fontSize: 11 }} value={name} onChange={(e) => setName(e.target.value)}>
        <option value="">— {t('character')} —</option>
        {characters.map((c) => (
          <option key={c.name} value={c.name}>
            {c.display_name || c.name}
          </option>
        ))}
      </select>
      <input
        type="number"
        className="ga-input"
        style={{ width: 60, fontSize: 11 }}
        min={1}
        value={qty}
        onChange={(e) => setQty(parseInt(e.target.value, 10) || 1)}
      />
      <button
        className="ga-btn ga-btn-sm ga-btn-primary"
        onClick={() => {
          if (name) {
            onAdd(name, qty)
            setName('')
            setQty(1)
          }
        }}
      >
        + {t('Give')}
      </button>
    </div>
  )
}
