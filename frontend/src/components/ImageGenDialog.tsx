import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/I18nProvider'
import { apiGet } from '../lib/api'

/**
 * Modal dialog for image-generation overrides — provider/workflow,
 * model, LoRAs, prompt. Pre-fills from `/world/imagegen-options`,
 * `/world/imagegen-models`, `/world/imagegen-loras`. Submit fires the
 * caller-supplied `onSubmit(payload)` and closes; the caller is
 * responsible for posting the request and refreshing the UI. The
 * server enqueues the job, so submit is fire-and-forget.
 */

interface LoraDefault {
  name: string
  strength: number
}

interface ImagegenOption {
  type: 'workflow' | 'backend'
  name: string
  label: string
  has_loras?: boolean
  default_loras?: LoraDefault[]
  model_type?: string
  default_model?: string
  filter?: string
  models?: string[] // for non-comfy backends with their own model list
}

interface ImagegenOptionsResponse {
  options: ImagegenOption[]
  default_location?: string
}

interface ImagegenModelsResponse {
  models: string[]
  models_by_service?: Record<string, string[]>
}

export interface ImageGenSubmit {
  prompt: string
  workflow?: string
  backend?: string
  model_override?: string
  loras?: LoraDefault[] | null
}

interface Props {
  open: boolean
  title: string
  defaultPrompt: string
  onSubmit: (payload: ImageGenSubmit) => void | Promise<void>
  onClose: () => void
}

const LORA_SLOTS = 4

function globToRegex(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')
  return new RegExp('^' + esc + '$', 'i')
}

function filterByWorkflowName(items: string[], workflowName: string, filter: string): string[] {
  if (filter) {
    const re = globToRegex(filter)
    const matched = items.filter((l) => {
      if (l === 'None') return true
      const base = l.includes('/') ? l.split('/').pop()! : l
      return re.test(base)
    })
    if (matched.length > 1) return matched
  } else if (workflowName) {
    const prefix = workflowName.toLowerCase()
    let matched = items.filter((l) => {
      if (l === 'None') return true
      const base = l.includes('/') ? l.split('/').pop()! : l
      return base.toLowerCase().startsWith(prefix)
    })
    if (matched.length <= 1) {
      matched = items.filter((l) => {
        if (l === 'None') return true
        const base = l.includes('/') ? l.split('/').pop()! : l
        return base.toLowerCase().includes(prefix)
      })
    }
    if (matched.length > 1) return matched
  }
  return items
}

export function ImageGenDialog({ open, title, defaultPrompt, onSubmit, onClose }: Props) {
  const { t } = useI18n()
  const [prompt, setPrompt] = useState(defaultPrompt)
  const [options, setOptions] = useState<ImagegenOption[] | null>(null)
  const [defaultLocationOpt, setDefaultLocationOpt] = useState<string>('')
  const [optionKey, setOptionKey] = useState<string>('') // "workflow:name" | "backend:name"
  const [modelData, setModelData] = useState<ImagegenModelsResponse | null>(null)
  const [allLoras, setAllLoras] = useState<string[]>([])
  const [modelOverride, setModelOverride] = useState<string>('')
  const [loraSlots, setLoraSlots] = useState<LoraDefault[]>(
    () => Array.from({ length: LORA_SLOTS }, () => ({ name: 'None', strength: 1.0 })),
  )
  const [submitting, setSubmitting] = useState(false)

  // Resync prompt when caller changes the default (e.g. day → night).
  useEffect(() => {
    if (open) setPrompt(defaultPrompt)
  }, [open, defaultPrompt])

  // Load options + loras once when dialog first opens.
  useEffect(() => {
    if (!open || options !== null) return
    apiGet<ImagegenOptionsResponse>('/world/imagegen-options')
      .then((d) => {
        setOptions(d.options || [])
        setDefaultLocationOpt(d.default_location || '')
      })
      .catch(() => setOptions([]))
    apiGet<{ loras: string[] }>('/world/imagegen-loras')
      .then((d) => setAllLoras(d.loras || []))
      .catch(() => setAllLoras([]))
  }, [open, options])

  // Pick initial option once the list arrives.
  useEffect(() => {
    if (!options || optionKey) return
    if (!options.length) return
    const match = defaultLocationOpt
      ? options.find(
          (o) =>
            `${o.type}:${o.name}` === defaultLocationOpt ||
            o.name === defaultLocationOpt,
        )
      : null
    const pick = match || options[0]
    setOptionKey(`${pick.type}:${pick.name}`)
  }, [options, defaultLocationOpt, optionKey])

  const currentOption = useMemo<ImagegenOption | null>(() => {
    if (!options || !optionKey) return null
    const [type, ...rest] = optionKey.split(':')
    const name = rest.join(':')
    return options.find((o) => o.type === type && o.name === name) || null
  }, [options, optionKey])

  // Fetch model list when the workflow's model_type changes; for backends
  // with their own models[] no fetch is needed.
  useEffect(() => {
    if (!currentOption) return
    if (currentOption.type === 'backend' && currentOption.models) {
      setModelData({ models: currentOption.models })
      setModelOverride(currentOption.default_model || currentOption.models[0] || '')
      return
    }
    if (currentOption.type === 'workflow' && currentOption.model_type) {
      apiGet<ImagegenModelsResponse>(
        `/world/imagegen-models?model_type=${encodeURIComponent(currentOption.model_type)}`,
      )
        .then((d) => {
          setModelData(d)
          setModelOverride(currentOption.default_model || '')
        })
        .catch(() => {
          setModelData({ models: [] })
          setModelOverride(currentOption.default_model || '')
        })
      return
    }
    setModelData(null)
    setModelOverride('')
  }, [currentOption])

  // Reset LoRA slots when workflow changes; pull defaults from the option.
  useEffect(() => {
    if (!currentOption) return
    const defaults = currentOption.default_loras || []
    setLoraSlots(
      Array.from({ length: LORA_SLOTS }, (_, i) => ({
        name: defaults[i]?.name || 'None',
        strength: defaults[i]?.strength ?? 1.0,
      })),
    )
  }, [currentOption])

  // ESC closes; lock body scroll while open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, submitting, onClose])

  const filteredLoras = useMemo(() => {
    if (!currentOption || !currentOption.has_loras) return []
    return filterByWorkflowName(allLoras, currentOption.name, currentOption.filter || '')
  }, [allLoras, currentOption])

  const handleSubmit = useCallback(async () => {
    if (!currentOption) return
    const payload: ImageGenSubmit = { prompt: prompt.trim() }
    if (currentOption.type === 'workflow') payload.workflow = currentOption.name
    else payload.backend = currentOption.name
    if (modelOverride) payload.model_override = modelOverride
    if (currentOption.has_loras) {
      const active = loraSlots.filter((l) => l.name && l.name !== 'None')
      payload.loras = active.length ? active : null
    }
    setSubmitting(true)
    try {
      await onSubmit(payload)
      onClose()
    } finally {
      setSubmitting(false)
    }
  }, [currentOption, prompt, modelOverride, loraSlots, onSubmit, onClose])

  if (!open) return null

  return (
    <div
      className="ga-modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose()
      }}
    >
      <div className="ga-modal" role="dialog" aria-label={title}>
        <div className="ga-modal-header">
          <span>{title}</span>
          <button
            className="ga-modal-close"
            onClick={onClose}
            disabled={submitting}
            aria-label={t('Close')}
          >
            ×
          </button>
        </div>
        <div className="ga-modal-body">
          {!options ? (
            <div className="ga-loading">{t('Loading…')}</div>
          ) : !options.length ? (
            <div className="ga-form-hint">{t('No image generation backends available.')}</div>
          ) : (
            <>
              <label className="ga-imagegen-label">{t('Service / Workflow')}</label>
              <select
                className="ga-input"
                value={optionKey}
                disabled={submitting}
                onChange={(e) => setOptionKey(e.target.value)}
              >
                {options.map((o) => (
                  <option key={`${o.type}:${o.name}`} value={`${o.type}:${o.name}`}>
                    {o.label}
                  </option>
                ))}
              </select>

              {modelData ? (
                <>
                  <label className="ga-imagegen-label">{t('Model')}</label>
                  <ModelSelect
                    data={modelData}
                    workflowName={currentOption?.name || ''}
                    filter={currentOption?.filter || ''}
                    value={modelOverride}
                    onChange={setModelOverride}
                    disabled={submitting}
                  />
                </>
              ) : null}

              {currentOption?.has_loras ? (
                <>
                  <label className="ga-imagegen-label">{t('LoRAs')}</label>
                  <div className="ga-imagegen-loras">
                    {loraSlots.map((slot, i) => {
                      const choices =
                        slot.name && slot.name !== 'None' && !filteredLoras.includes(slot.name)
                          ? [slot.name, ...filteredLoras]
                          : filteredLoras
                      return (
                        <div key={i} className="ga-imagegen-lora-row">
                          <span className="ga-imagegen-lora-label">LoRA {i + 1}</span>
                          <select
                            className="ga-input"
                            value={slot.name}
                            disabled={submitting}
                            onChange={(e) =>
                              setLoraSlots((prev) =>
                                prev.map((s, idx) =>
                                  idx === i ? { ...s, name: e.target.value } : s,
                                ),
                              )
                            }
                          >
                            {choices.map((l) => (
                              <option key={l} value={l}>
                                {l}
                              </option>
                            ))}
                          </select>
                          <input
                            type="number"
                            className="ga-input ga-imagegen-lora-strength"
                            min={-2}
                            max={2}
                            step={0.05}
                            disabled={submitting || slot.name === 'None'}
                            value={slot.strength}
                            onChange={(e) =>
                              setLoraSlots((prev) =>
                                prev.map((s, idx) =>
                                  idx === i
                                    ? { ...s, strength: parseFloat(e.target.value) || 0 }
                                    : s,
                                ),
                              )
                            }
                          />
                        </div>
                      )
                    })}
                  </div>
                </>
              ) : null}

              <label className="ga-imagegen-label">{t('Prompt')}</label>
              <textarea
                className="ga-textarea"
                rows={6}
                value={prompt}
                disabled={submitting}
                onChange={(e) => setPrompt(e.target.value)}
              />
            </>
          )}
        </div>
        <div className="ga-modal-footer">
          <button className="ga-btn" onClick={onClose} disabled={submitting}>
            {t('Cancel')}
          </button>
          <button
            className="ga-btn ga-btn-primary"
            onClick={handleSubmit}
            disabled={submitting || !currentOption}
          >
            {submitting ? '…' : t('Generate')}
          </button>
        </div>
      </div>
    </div>
  )
}

function ModelSelect({
  data,
  workflowName,
  filter,
  value,
  onChange,
  disabled,
}: {
  data: ImagegenModelsResponse
  workflowName: string
  filter: string
  value: string
  onChange: (v: string) => void
  disabled?: boolean
}) {
  const { byService, flat } = useMemo(() => {
    const svc = data.models_by_service && Object.keys(data.models_by_service).length
      ? data.models_by_service
      : null
    if (svc) {
      const grouped: Record<string, string[]> = {}
      for (const [k, v] of Object.entries(svc)) {
        const filtered = filterByWorkflowName(v, workflowName, filter)
        if (filtered.length) grouped[k] = filtered
      }
      return { byService: grouped, flat: null }
    }
    return { byService: null, flat: filterByWorkflowName(data.models, workflowName, filter) }
  }, [data, workflowName, filter])

  const allValues = byService
    ? Object.values(byService).flat()
    : flat || []
  // Always include the current value as an option even if filtered out.
  const includesCurrent = !value || allValues.includes(value)

  return (
    <select
      className="ga-input"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    >
      {!includesCurrent ? <option value={value}>{value}</option> : null}
      {byService
        ? Object.entries(byService)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([svc, models]) => (
              <optgroup key={svc} label={svc}>
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </optgroup>
            ))
        : (flat || []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
    </select>
  )
}
