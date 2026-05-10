import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiGet, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'

/**
 * Game-Admin "Setup" tab — a single multi-line description of the world
 * (its tone, era, genre, ground rules, etc.) that the chat / World-Dev
 * LLMs see before any character or location context. The text is
 * injected into the templates via the `world_setup` / `{world_setup_block}`
 * variables — never hard-coded in the prompts themselves.
 */
export function SetupTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [description, setDescription] = useState('')
  const [original, setOriginal] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const d = await apiGet<{ description?: string }>('/admin/world-setup')
      const text = d.description || ''
      setDescription(text)
      setOriginal(text)
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    } finally {
      setLoading(false)
    }
  }, [t, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const save = useCallback(async () => {
    setSaving(true)
    try {
      await apiPut('/admin/world-setup', { description })
      setOriginal(description)
      toast(t('Saved'))
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    } finally {
      setSaving(false)
    }
  }, [description, t, toast])

  if (loading) return <div className="ga-loading">{t('Loading…')}</div>

  const dirty = description !== original

  return (
    <div className="ga-page-scroll">
      <DetailToolbar
        title={dirty ? t('Setup (unsaved)') : t('Setup')}
        onSave={save}
        onCancel={dirty ? () => setDescription(original) : undefined}
        disabled={saving}
        cancelLabel={t('Revert')}
      />
      <div className="ga-form" style={{ maxWidth: 1100 }}>
        <Field
          label={t('World setup')}
          hint={t(
            'Free-form description of the world: tone, era, genre, ground rules. The chat and World-Dev LLMs see this as a briefing before any character or location context. Empty = no world briefing.',
          )}
        >
          <textarea
            className="ga-textarea"
            rows={20}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t(
              'e.g. "Modern-day Berlin. Adults only. Slice-of-life with occasional supernatural twists. Characters speak everyday German; English fine for slang."',
            )}
            spellCheck
          />
        </Field>
        <div className="ga-form-hint">
          {t('Length: characters')} {description.length.toLocaleString()}
        </div>
      </div>
    </div>
  )
}
