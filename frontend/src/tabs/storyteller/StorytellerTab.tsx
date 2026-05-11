import { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiGet, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'
import { Field } from '../../components/Field'
import { DetailToolbar } from '../../components/DetailToolbar'

/**
 * Game-Admin "Storyteller" tab — per-world config for the act-skill
 * pipeline. The Storyteller is not a character: it's the engine that
 * narrates the consequence of an in-world action and decides which
 * skills the action triggers. The config controls the chat mode and the
 * skill whitelist used by the StreamingAgent inside `act_skill.py`.
 */
interface StorytellerConfig {
  chat_mode: 'rp_first' | 'single' | 'no_tools'
  llm_task: string
  enabled_skills: Record<string, boolean>
}

interface ConfigResponse {
  config: StorytellerConfig
  skill_keys: string[]
}

const SKILL_LABELS: Record<string, string> = {
  outfit_change: 'Change Outfit',
  outfit_creation: 'Create New Outfit',
  imagegen: 'Image Generator',
  videogen: 'Video Generator',
  setactivity: 'Set Activity',
  setlocation: 'Set Location',
  consume_item: 'Consume Item',
  describe_room: 'Describe Room',
  talk_to: 'Talk To',
  send_message: 'Send Message',
  act: 'Act (recursive)',
  instagram: 'Instagram Post',
  instagram_comment: 'Instagram Comment',
  instagram_reply: 'Instagram Reply',
  markdown_writer: 'Write Markdown',
  retrospect: 'Retrospect',
  notify_user: 'Notify User',
}

const SKILL_HINTS: Record<string, string> = {
  outfit_change: 'Lets the storyteller change the subject\'s outfit when the action implies dressing/undressing.',
  outfit_creation: 'Lets the storyteller create a brand new outfit when nothing fitting exists.',
  imagegen: 'Lets the storyteller capture the scene as an image.',
  videogen: 'Lets the storyteller record a short video of the scene.',
  setactivity: 'Lets the storyteller adjust the subject\'s current activity (e.g. "sits at the lake").',
  setlocation: 'Usually OFF — the player moves via D-pad/map, not via Action.',
  consume_item: 'Lets the storyteller mark inventory items as consumed (food, potions).',
  describe_room: 'Lets the storyteller produce a separate room description — usually redundant.',
  talk_to: 'Usually OFF — Action is monologue/handling, not dialogue.',
  send_message: 'Usually OFF — Action is monologue/handling, not dialogue.',
  act: 'Recursive Act invocation — keep OFF to avoid loops.',
  instagram: 'Usually OFF — Action does not auto-publish to social media.',
  instagram_comment: 'Usually OFF.',
  instagram_reply: 'Usually OFF.',
  markdown_writer: 'Usually OFF — diary writing is separate.',
  retrospect: 'Usually OFF — reflection is its own flow.',
  notify_user: 'Usually OFF.',
}

export function StorytellerTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [config, setConfig] = useState<StorytellerConfig | null>(null)
  const [original, setOriginal] = useState<StorytellerConfig | null>(null)
  const [skillKeys, setSkillKeys] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const d = await apiGet<ConfigResponse>('/admin/storyteller/config')
      setConfig(d.config)
      setOriginal(d.config)
      setSkillKeys(d.skill_keys || [])
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
    if (!config) return
    setSaving(true)
    try {
      const d = await apiPut<ConfigResponse>('/admin/storyteller/config', config)
      setConfig(d.config)
      setOriginal(d.config)
      toast(t('Saved'))
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    } finally {
      setSaving(false)
    }
  }, [config, t, toast])

  const dirty = useMemo(() => {
    if (!config || !original) return false
    return JSON.stringify(config) !== JSON.stringify(original)
  }, [config, original])

  const setChatMode = useCallback((mode: StorytellerConfig['chat_mode']) => {
    setConfig((prev) => (prev ? { ...prev, chat_mode: mode } : prev))
  }, [])

  const toggleSkill = useCallback((sid: string, enabled: boolean) => {
    setConfig((prev) => prev
      ? { ...prev, enabled_skills: { ...prev.enabled_skills, [sid]: enabled } }
      : prev)
  }, [])

  if (loading || !config) return <div className="ga-loading">{t('Loading…')}</div>

  return (
    <div className="ga-page-scroll">
      <DetailToolbar
        title={dirty ? t('Storyteller (unsaved)') : t('Storyteller')}
        onSave={save}
        onCancel={dirty && original ? () => setConfig(original) : undefined}
        disabled={saving}
        cancelLabel={t('Revert')}
      />
      <div className="ga-form" style={{ maxWidth: 1100 }}>
        <div className="ga-form-hint" style={{ marginBottom: 16 }}>
          {t('The Storyteller is the engine behind the player\'s 🎭 Action button. It narrates the consequence of an in-world action and decides which skills fire (e.g. ChangeOutfit when the player undresses). Skills run as the acting subject, not as a separate character.')}
        </div>

        <Field
          label={t('Chat mode')}
          hint={t('rp_first = narrate first, then a tool-LLM decides which skills fire (recommended). single = one LLM does both. no_tools = narration only, no skills.')}
        >
          <select
            className="ga-input"
            value={config.chat_mode}
            onChange={(e) => setChatMode(e.target.value as StorytellerConfig['chat_mode'])}
            style={{ maxWidth: 320 }}
          >
            <option value="rp_first">rp_first ({t('two-pass — recommended')})</option>
            <option value="single">single ({t('one-pass')})</option>
            <option value="no_tools">no_tools ({t('narration only')})</option>
          </select>
        </Field>

        <Field
          label={t('LLM task')}
          hint={t('Routing key used to pick the LLM (see Admin Settings → LLM Routing). Keep "storyteller" unless you want to share routing with another task.')}
        >
          <input
            className="ga-input"
            value={config.llm_task}
            onChange={(e) => setConfig((prev) => prev ? { ...prev, llm_task: e.target.value } : prev)}
            style={{ maxWidth: 320 }}
          />
        </Field>

        <div className="ga-section-header" style={{ marginTop: 24, marginBottom: 8 }}>
          {t('Enabled skills')}
        </div>
        <div className="ga-form-hint" style={{ marginBottom: 12 }}>
          {t('Only the skills enabled here will be offered to the Storyteller\'s tool-LLM. Disabled skills are simply ignored — they still work in normal chat with characters.')}
        </div>

        <div className="ga-grid-2" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 8 }}>
          {skillKeys.map((sid) => {
            const enabled = !!config.enabled_skills[sid]
            return (
              <label key={sid} className="ga-checkbox-row" style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '8px 10px', background: 'var(--bg-container)', border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => toggleSkill(sid, e.target.checked)}
                  style={{ marginTop: 2 }}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{t(SKILL_LABELS[sid] || sid)}</div>
                  {SKILL_HINTS[sid] ? (
                    <div className="ga-form-hint" style={{ fontSize: 12, marginTop: 2 }}>
                      {t(SKILL_HINTS[sid])}
                    </div>
                  ) : null}
                </div>
              </label>
            )
          })}
        </div>

        <div className="ga-form-hint" style={{ marginTop: 24 }}>
          {t('To edit the storyteller prompt itself, open ')}
          <a href="/admin/templates" target="_blank" rel="noreferrer">/admin/templates</a>
          {t(' and pick ')}<code>tasks/storyteller_react.md</code>.{' '}
          {t('To pick the LLM model that backs this task, open ')}
          <a href="/admin/settings" target="_blank" rel="noreferrer">/admin/settings → LLM Routing</a>.
        </div>
      </div>
    </div>
  )
}
