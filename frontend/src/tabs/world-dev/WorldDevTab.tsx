import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiGet, apiPost } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { loadCharacters, loadLocations, type CharacterRef, type LocationRef } from '../../lib/refs'

interface ModelEntry {
  name: string
  provider?: string
  /** Per 1M tokens, in USD. Local / unpriced models leave these at 0. */
  pricing?: { input?: number; output?: number }
}

interface UsageStats {
  tokens_in: number
  tokens_out: number
  cost_total: number
}

function formatUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

interface SchemaInfo {
  name: string
  label: string
}

type Mode = 'new' | 'edit'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

interface ExtractedData {
  location_data?: Record<string, unknown>
  character_data?: Record<string, unknown>
  outfit_data?: Record<string, unknown>
  soul_data?: Record<string, unknown>
  profile_patch_data?: Record<string, unknown>
}

interface TemplateInfo {
  name: string
  label: string
}

export function WorldDevTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [models, setModels] = useState<ModelEntry[]>([])
  const [schemas, setSchemas] = useState<SchemaInfo[]>([])
  const [templates, setTemplates] = useState<TemplateInfo[]>([])
  const [characters, setCharacters] = useState<CharacterRef[]>([])
  const [locations, setLocations] = useState<LocationRef[]>([])
  const [model, setModel] = useState('')
  const [provider, setProvider] = useState('')
  // Separate model picker for the JSON validator. Defaults to the chat
  // model when empty so users get a sane fallback without a second pick;
  // can be set to a smaller / cheaper model independently.
  const [validateModel, setValidateModel] = useState('')
  const [validateProvider, setValidateProvider] = useState('')
  const [schema, setSchema] = useState<string>('location')
  const [mode, setMode] = useState<Mode>('new')
  const [template, setTemplate] = useState<string>('human-roleplay')
  const [editTarget, setEditTarget] = useState('')
  const [contextLocations, setContextLocations] = useState<Set<string>>(new Set())
  const [contextCharacters, setContextCharacters] = useState<Set<string>>(new Set())

  const [sessionId, setSessionId] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState(false)
  const [pending, setPending] = useState('')
  const [extracted, setExtracted] = useState<ExtractedData>({})
  const [draft, setDraft] = useState('')
  const [usage, setUsage] = useState<UsageStats | null>(null)

  const chatScrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    apiGet<{ providers?: Record<string, { models?: ModelEntry[] }> }>('/characters/available-models')
      .then((d) => {
        const flat: ModelEntry[] = []
        for (const [provName, prov] of Object.entries(d.providers || {})) {
          for (const m of prov.models || []) {
            // Carry pricing through — provider-flat list dropped it before
            // and that's why the dropdown never showed $-prices.
            flat.push({ name: m.name, provider: provName, pricing: m.pricing })
          }
        }
        setModels(flat)
      })
      .catch(() => setModels([]))
    apiGet<{ schemas?: SchemaInfo[] }>('/world-dev/schemas')
      .then((d) => setSchemas(d.schemas || []))
      .catch(() => setSchemas([]))
    apiGet<{ templates?: Array<{ name: string; label?: string }> }>('/world-dev/character-templates')
      .then((d) => {
        const list = (d.templates || []).map((t) => ({ name: t.name, label: t.label || t.name }))
        setTemplates(list)
        // Switch the default template if the previous hard-coded one is
        // no longer in the list (e.g. project removed `human-roleplay`).
        if (list.length && !list.find((t) => t.name === template)) {
          setTemplate(list[0].name)
        }
      })
      .catch(() => setTemplates([]))
    loadCharacters().then(setCharacters).catch(() => setCharacters([]))
    loadLocations().then(setLocations).catch(() => setLocations([]))
  }, [])

  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight
    }
  }, [messages, pending])

  const newSession = useCallback(() => {
    setSessionId('')
    setMessages([])
    setPending('')
    setExtracted({})
    setDraft('')
    setUsage(null)
  }, [])

  const send = useCallback(async () => {
    if (!model) {
      toast(t('Pick a model first'), 'error')
      return
    }
    if (!draft.trim()) return
    if (streaming) return

    const userMsg = draft.trim()
    setMessages((prev) => [...prev, { role: 'user', content: userMsg }])
    setDraft('')
    setPending('')
    setStreaming(true)
    setExtracted({})

    try {
      const res = await fetch('/world-dev/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          model,
          provider,
          session_id: sessionId,
          message: userMsg,
          schema,
          character_template: schema === 'character' ? template : '',
          edit_location_id: mode === 'edit' ? editTarget : '',
          context_location_ids: Array.from(contextLocations),
          context_character_names: Array.from(contextCharacters),
        }),
      })
      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => '')
        toast(t('Chat failed') + ': ' + (text || `HTTP ${res.status}`), 'error')
        setStreaming(false)
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let acc = ''
      const localExtracted: ExtractedData = {}

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, idx).trim()
          buffer = buffer.slice(idx + 2)
          if (!chunk.startsWith('data:')) continue
          const json = chunk.slice(5).trim()
          if (!json) continue
          try {
            const evt = JSON.parse(json)
            if (evt.session_id) setSessionId(evt.session_id)
            if (evt.content) {
              acc += evt.content
              setPending(acc)
            }
            if (evt.usage && typeof evt.usage === 'object') {
              setUsage({
                tokens_in: Number(evt.usage.tokens_in) || 0,
                tokens_out: Number(evt.usage.tokens_out) || 0,
                cost_total: Number(evt.usage.cost_total) || 0,
              })
            }
            for (const k of [
              'location_data',
              'character_data',
              'outfit_data',
              'soul_data',
              'profile_patch_data',
            ] as const) {
              if (evt[k]) localExtracted[k] = evt[k]
            }
          } catch {
            /* drop malformed chunks */
          }
        }
      }

      setMessages((prev) => [...prev, { role: 'assistant', content: acc }])
      setPending('')
      setExtracted(localExtracted)
    } catch (e) {
      toast(t('Chat failed') + ': ' + (e as Error).message, 'error')
    } finally {
      setStreaming(false)
    }
  }, [contextCharacters, contextLocations, draft, editTarget, mode, model, provider, schema, sessionId, streaming, t, template, toast])

  const apply = useCallback(
    async (kind: keyof ExtractedData) => {
      const data = extracted[kind]
      if (!data) return
      // Each apply endpoint expects a different body shape:
      //   /apply          → { location_data: {...} }
      //   /apply-character→ { character_data: {...} }
      //   /apply-outfit   → flat: { character_name, outfit }
      //   /apply-soul     → flat: { character_name, section, content }
      //   /apply-profile-patch → flat: { character_name, fields }
      let path = ''
      let body: Record<string, unknown> = { session_id: sessionId }
      switch (kind) {
        case 'location_data':
          path = '/world-dev/apply'
          body = { ...body, location_data: data }
          break
        case 'character_data':
          path = '/world-dev/apply-character'
          body = { ...body, character_data: data }
          break
        case 'outfit_data':
          path = '/world-dev/apply-outfit'
          body = { ...body, ...(data as object) }
          break
        case 'soul_data':
          path = '/world-dev/apply-soul'
          body = { ...body, ...(data as object) }
          break
        case 'profile_patch_data':
          path = '/world-dev/apply-profile-patch'
          body = { ...body, ...(data as object) }
          break
      }
      try {
        await apiPost(path, body)
        toast(t('Applied'))
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [extracted, sessionId, t, toast],
  )

  // Validate the most recent extracted JSON via a tool LLM. Result is
  // a plain-text bullet list of missing/incomplete fields, dropped into
  // the chat input so the user can hit Send to ask the RP LLM to fill
  // them in.
  const [validating, setValidating] = useState(false)
  const validate = useCallback(async () => {
    // Pick whichever extracted block is most informative — character or
    // location are the typical cases for a "fill the gaps" prompt.
    const data =
      extracted.character_data ||
      extracted.location_data ||
      extracted.outfit_data ||
      extracted.soul_data ||
      extracted.profile_patch_data
    if (!data) return
    const detected =
      extracted.character_data ? 'character'
      : extracted.location_data ? 'location'
      : schema
    // Use the explicit validator model if the user picked one, otherwise
    // fall back to the chat model so the feature works out of the box.
    const vModel = validateModel || model
    const vProvider = validateModel ? validateProvider : provider
    if (!vModel) {
      toast(t('Pick a model first'), 'error')
      return
    }
    setValidating(true)
    try {
      const res = await apiPost<{ gaps?: string }>('/world-dev/validate-json', {
        schema: detected,
        data,
        model: vModel,
        provider: vProvider,
      })
      const gaps = (res.gaps || '').trim()
      if (!gaps || gaps === 'OK') {
        toast(t('Validation: no gaps detected'))
        return
      }
      const prompt =
        t('Please complete the following missing or incomplete fields:') + '\n\n' + gaps
      // Append to existing draft so user keeps any in-flight text.
      setDraft((prev) => (prev ? `${prev.trim()}\n\n${prompt}` : prompt))
      toast(t('Gaps written into the input'))
    } catch (e) {
      toast(t('Validation failed') + ': ' + (e as Error).message, 'error')
    } finally {
      setValidating(false)
    }
  }, [extracted, schema, model, provider, validateModel, validateProvider, t, toast])

  const targetOptions = useMemo(() => {
    if (schema === 'character') return characters.map((c) => ({ id: c.name, label: c.display_name || c.name }))
    return locations.map((l) => ({ id: l.id, label: l.name || l.id }))
  }, [characters, locations, schema])

  const toggleContextLocation = (id: string) => {
    setContextLocations((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleContextCharacter = (name: string) => {
    setContextCharacters((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  // Filter "Durchgangs-Locations" (passable=true: corridors, doorways,
  // any place that exists only as a transit node) — they're not real
  // contexts the LLM should reason about. Locations without an explicit
  // `passable` flag default to non-passable and stay visible.
  const contextLocationOptions = locations.filter((l) => l.passable !== true)
  const remainingLocations = contextLocationOptions.filter((l) => !contextLocations.has(l.id))
  const remainingCharacters = characters.filter((c) => !contextCharacters.has(c.name))

  return (
    <div className="ga-wd-shell">
      <div className="ga-wd-config">
        <div className="ga-wd-config-row">
          <button className="ga-btn ga-btn-sm" onClick={newSession} title={t('Start a fresh conversation')}>
            ↻ {t('New conversation')}
          </button>
          <select
            className="ga-input ga-wd-model-select"
            value={model ? `${provider}|${model}` : ''}
            onChange={(e) => {
              const v = e.target.value
              if (!v) {
                setModel('')
                setProvider('')
                return
              }
              const [prov, name] = v.split('|', 2)
              setProvider(prov || '')
              setModel(name || '')
            }}
          >
            <option value="">— {t('Pick model')} —</option>
            {(() => {
              const byProvider = new Map<string, ModelEntry[]>()
              for (const m of models) {
                const key = m.provider || ''
                const arr = byProvider.get(key) || []
                arr.push(m)
                byProvider.set(key, arr)
              }
              const providers = Array.from(byProvider.keys()).sort()
              return providers.map((prov) => (
                <optgroup key={prov} label={prov || t('Other')}>
                  {byProvider.get(prov)!.map((m) => {
                    const inP = m.pricing?.input || 0
                    const outP = m.pricing?.output || 0
                    // Format prices per 1M tokens. Local / free models
                    // (zero pricing) get no suffix. External providers
                    // typically charge in $/M tokens.
                    let priceTag = ''
                    if (inP > 0 || outP > 0) {
                      const fmt = (v: number) =>
                        v >= 1 ? v.toFixed(2) : v.toFixed(2).replace(/\.?0+$/, '') || v.toFixed(2)
                      priceTag = `  ·  $${fmt(inP)} / $${fmt(outP)} per 1M`
                    }
                    return (
                      <option key={`${prov}|${m.name}`} value={`${prov}|${m.name}`}>
                        {m.name}
                        {priceTag}
                      </option>
                    )
                  })}
                </optgroup>
              ))
            })()}
          </select>
          <select
            className="ga-input ga-wd-model-select"
            value={validateModel ? `${validateProvider}|${validateModel}` : ''}
            onChange={(e) => {
              const v = e.target.value
              if (!v) {
                setValidateModel('')
                setValidateProvider('')
                return
              }
              const [prov, name] = v.split('|', 2)
              setValidateProvider(prov || '')
              setValidateModel(name || '')
            }}
            title={t('Model used by the Validate button. Empty = same as chat model.')}
          >
            <option value="">— {t('Validator: same as chat')} —</option>
            {(() => {
              const byProvider = new Map<string, ModelEntry[]>()
              for (const m of models) {
                const key = m.provider || ''
                const arr = byProvider.get(key) || []
                arr.push(m)
                byProvider.set(key, arr)
              }
              const providers = Array.from(byProvider.keys()).sort()
              return providers.map((prov) => (
                <optgroup key={prov} label={prov || t('Other')}>
                  {byProvider.get(prov)!.map((m) => {
                    const inP = m.pricing?.input || 0
                    const outP = m.pricing?.output || 0
                    let priceTag = ''
                    if (inP > 0 || outP > 0) {
                      const fmt = (v: number) =>
                        v >= 1 ? v.toFixed(2) : v.toFixed(2).replace(/\.?0+$/, '') || v.toFixed(2)
                      priceTag = `  ·  $${fmt(inP)} / $${fmt(outP)} per 1M`
                    }
                    return (
                      <option key={`v-${prov}|${m.name}`} value={`${prov}|${m.name}`}>
                        {m.name}
                        {priceTag}
                      </option>
                    )
                  })}
                </optgroup>
              ))
            })()}
          </select>
          <select
            className="ga-input ga-wd-compact-select"
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as Mode)
              setEditTarget('')
            }}
            title={t('Mode')}
          >
            <option value="new">{t('Create new')}</option>
            <option value="edit">{t('Edit')}</option>
          </select>
          <select
            className="ga-input ga-wd-compact-select"
            value={schema}
            onChange={(e) => setSchema(e.target.value)}
            title={t('Schema')}
          >
            {(schemas.length ? schemas : [{ name: 'location', label: 'Location' }]).map((s) => (
              <option key={s.name} value={s.name}>
                {s.label}
              </option>
            ))}
          </select>
          {mode === 'new' && schema === 'character' ? (
            <select
              className="ga-input ga-wd-compact-select"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              title={t('Template')}
            >
              {templates.length === 0 ? (
                <option value="">— {t('no templates')} —</option>
              ) : null}
              {templates.map((tp) => (
                <option key={tp.name} value={tp.name}>
                  {tp.label}
                </option>
              ))}
            </select>
          ) : null}
          {mode === 'edit' ? (
            <select
              className="ga-input ga-wd-target-select"
              value={editTarget}
              onChange={(e) => setEditTarget(e.target.value)}
              title={t('Target to edit')}
            >
              <option value="">— {t('select target')} —</option>
              {targetOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          ) : null}
        </div>

        <div className="ga-wd-context-row">
          <span className="ga-wd-context-label">{t('Locations')}</span>
          <div className="ga-tags-row ga-wd-tags">
            {Array.from(contextLocations).map((id) => {
              const loc = locations.find((l) => l.id === id)
              return (
                <button
                  key={id}
                  type="button"
                  className="ga-tag-pill"
                  onClick={() => toggleContextLocation(id)}
                >
                  {loc?.name || id} ×
                </button>
              )
            })}
            <select
              className="ga-input ga-wd-tag-add"
              value=""
              onChange={(e) => {
                if (e.target.value) toggleContextLocation(e.target.value)
              }}
            >
              <option value="">+ {t('add location')}</option>
              {remainingLocations.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name || l.id}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="ga-wd-context-row">
          <span className="ga-wd-context-label">{t('Characters')}</span>
          <div className="ga-tags-row ga-wd-tags">
            {Array.from(contextCharacters).map((name) => {
              const c = characters.find((x) => x.name === name)
              return (
                <button
                  key={name}
                  type="button"
                  className="ga-tag-pill"
                  onClick={() => toggleContextCharacter(name)}
                >
                  {c?.display_name || name} ×
                </button>
              )
            })}
            <select
              className="ga-input ga-wd-tag-add"
              value=""
              onChange={(e) => {
                if (e.target.value) toggleContextCharacter(e.target.value)
              }}
            >
              <option value="">+ {t('add character')}</option>
              {remainingCharacters.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.display_name || c.name}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="ga-wd-chat" ref={chatScrollRef}>
        {messages.length === 0 && !pending ? (
          <div className="ga-placeholder">
            {mode === 'edit' && editTarget
              ? t('Editing "{name}". Describe your changes below and click Send.').replace(
                  '{name}',
                  targetOptions.find((o) => o.id === editTarget)?.label || editTarget,
                )
              : t('Pick a model and describe what you want to create or edit.')}
          </div>
        ) : null}
        {messages.map((m, idx) => (
          <div key={idx} className={`ga-wd-msg ga-wd-msg-${m.role}`}>
            <div className="ga-wd-msg-role">{m.role === 'user' ? t('You') : t('Assistant')}</div>
            <pre className="ga-wd-msg-body">{m.content}</pre>
          </div>
        ))}
        {pending ? (
          <div className="ga-wd-msg ga-wd-msg-assistant">
            <div className="ga-wd-msg-role">{t('Assistant')}</div>
            <pre className="ga-wd-msg-body">{pending}</pre>
          </div>
        ) : null}
      </div>

      {Object.keys(extracted).length > 0 ? (
        <div className="ga-wd-extracted">
          <div className="ga-form-section-label">{t('Extracted JSON')}</div>
          <div className="ga-form-row">
            {(Object.keys(extracted) as Array<keyof ExtractedData>).map((k) => (
              <button key={k} className="ga-btn ga-btn-primary ga-btn-sm" onClick={() => apply(k)}>
                {t('Apply')} {k.replace('_data', '')}
              </button>
            ))}
            <button
              className="ga-btn ga-btn-sm"
              onClick={validate}
              disabled={validating}
              title={t('Run a tool LLM over the JSON and write missing fields into the input below')}
            >
              {validating ? t('Validating…') : t('Validate (find gaps)')}
            </button>
          </div>
        </div>
      ) : null}

      <div className="ga-wd-input">
        <textarea
          className="ga-textarea"
          rows={3}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t('Describe what you want to create or edit… Enter to send, Shift+Enter for newline')}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
          disabled={streaming}
        />
        <div className="ga-wd-stats" title={t('Session totals — accumulated across all turns of this conversation')}>
          {usage ? (
            <>
              <span>
                <strong>{t('In')}</strong> {usage.tokens_in.toLocaleString()}
              </span>
              <span>
                <strong>{t('Out')}</strong> {usage.tokens_out.toLocaleString()}
              </span>
              <span>
                <strong>{t('Cost')}</strong> {formatUsd(usage.cost_total)}
              </span>
            </>
          ) : (
            <span className="ga-form-hint">{t('No session yet')}</span>
          )}
        </div>
        <button className="ga-btn ga-btn-primary" onClick={send} disabled={streaming || !draft.trim()}>
          {streaming ? t('…') : t('Send')}
        </button>
      </div>
    </div>
  )
}
