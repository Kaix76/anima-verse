import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPost, apiPut } from '../../lib/api'
import { useToast } from '../../lib/Toast'

interface Job {
  id?: string
  agent?: string
  character?: string
  trigger?: Record<string, unknown>
  action?: { type?: string; [k: string]: unknown }
  enabled?: boolean
}

type TriggerKind = 'cron-hourly' | 'cron-daily' | 'interval-minutes' | 'date'
type ActionKind = 'extract_files' | 'notify'

interface FormState {
  trigger: TriggerKind
  extra: string
  action: ActionKind
  payload: string
  agent: string
}

const POLL_INTERVAL_MS = 15_000

const INITIAL_FORM: FormState = {
  trigger: 'cron-hourly',
  extra: '',
  action: 'extract_files',
  payload: '',
  agent: '',
}

function buildTrigger(form: FormState): Record<string, unknown> {
  const extra = form.extra.trim()
  switch (form.trigger) {
    case 'cron-hourly':
      return { type: 'cron', minute: 0 }
    case 'cron-daily': {
      const m = extra.match(/^(\d{1,2}):(\d{2})$/)
      const hour = m ? parseInt(m[1], 10) : 3
      const minute = m ? parseInt(m[2], 10) : 0
      return { type: 'cron', hour, minute }
    }
    case 'interval-minutes':
      return { type: 'interval', minutes: parseInt(extra, 10) || 30 }
    case 'date':
      return { type: 'date', run_date: extra }
  }
}

function buildAction(form: FormState): Record<string, unknown> {
  const payload = form.payload.trim()
  if (form.action === 'extract_files') {
    return { type: 'extract_files', extraction_prompt: payload }
  }
  return { type: 'notify', message: payload || 'admin notify' }
}

export function SchedulerTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [submitting, setSubmitting] = useState(false)

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ data?: Job[] }>('/scheduler/jobs')
      setJobs(data.data || [])
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    reload()
    const id = window.setInterval(reload, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [reload])

  const handleDelete = useCallback(
    async (id: string) => {
      if (!window.confirm(t('Delete job {id}?').replace('{id}', id))) return
      try {
        await apiDelete(`/scheduler/jobs/${encodeURIComponent(id)}`)
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
      await reload()
    },
    [reload, t, toast],
  )

  const handleToggle = useCallback(
    async (id: string) => {
      try {
        await apiPut(`/scheduler/jobs/${encodeURIComponent(id)}/toggle`, {})
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
      await reload()
    },
    [reload, t, toast],
  )

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      setSubmitting(true)
      try {
        await apiPost('/scheduler/jobs', {
          agent: form.agent.trim(),
          trigger: buildTrigger(form),
          action: buildAction(form),
          enabled: true,
        })
        setForm({ ...INITIAL_FORM, trigger: form.trigger, action: form.action })
        toast(t('Job created'))
        await reload()
      } catch (err) {
        toast(t('Create failed') + ': ' + (err as Error).message, 'error')
      } finally {
        setSubmitting(false)
      }
    },
    [form, reload, t, toast],
  )

  return (
    <div className="ga-page-scroll">
      <h2 style={{ fontSize: 16, marginBottom: 6 }}>{t('Scheduler — Background Jobs')}</h2>

      <section className="ga-sched-section">
        <h3>{t('All jobs')}</h3>
        <p className="ga-sched-muted">
          {t(
            'Admin jobs (e.g. memory consolidation, file extraction) are highlighted as "admin". Per-character jobs from the legacy scheduler still surface here for visibility and can be deleted, but should no longer be created — character actions belong in the AgentLoop.',
          )}
        </p>
        <table className="ga-sched-table">
          <thead>
            <tr>
              <th>{t('Job ID')}</th>
              <th>{t('Owner')}</th>
              <th>{t('Trigger')}</th>
              <th>{t('Action')}</th>
              <th>{t('Status')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {error ? (
              <tr>
                <td colSpan={6}>error: {error}</td>
              </tr>
            ) : jobs === null ? (
              <tr>
                <td colSpan={6} className="ga-sched-muted">
                  {t('Loading…')}
                </td>
              </tr>
            ) : jobs.length === 0 ? (
              <tr>
                <td colSpan={6} className="ga-sched-muted">
                  {t('No jobs scheduled.')}
                </td>
              </tr>
            ) : (
              jobs.map((job) => {
                const owner = (job.agent || job.character || '').trim()
                const enabled = job.enabled !== false
                const trig = job.trigger ? JSON.stringify(job.trigger).slice(0, 80) : ''
                const id = job.id || ''
                return (
                  <tr key={id}>
                    <td>{id || '?'}</td>
                    <td>
                      {owner ? (
                        <span className="ga-tag ga-tag-char">{owner}</span>
                      ) : (
                        <span className="ga-tag ga-tag-admin">admin</span>
                      )}
                    </td>
                    <td>{trig}</td>
                    <td>{job.action?.type || '?'}</td>
                    <td className={enabled ? 'ga-status-ok' : 'ga-status-paused'}>
                      {enabled ? t('enabled') : t('paused')}
                    </td>
                    <td className="ga-or-actions-col">
                      <button className="ga-btn ga-btn-sm" onClick={() => handleToggle(id)}>
                        {enabled ? t('Pause') : t('Resume')}
                      </button>{' '}
                      <button
                        className="ga-btn ga-btn-sm ga-btn-danger"
                        onClick={() => handleDelete(id)}
                      >
                        {t('Delete')}
                      </button>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </section>

      <section className="ga-sched-section">
        <h3>{t('Create admin job')}</h3>
        <form className="ga-sched-form" onSubmit={handleSubmit}>
          <div className="ga-sched-form-row">
            <div className="ga-sched-field">
              <label>{t('Trigger')}</label>
              <select
                className="ga-input"
                value={form.trigger}
                onChange={(e) => setForm((f) => ({ ...f, trigger: e.target.value as TriggerKind }))}
                required
              >
                <option value="cron-hourly">{t('Every hour at :00')}</option>
                <option value="cron-daily">{t('Once a day')}</option>
                <option value="interval-minutes">{t('Every N minutes')}</option>
                <option value="date">{t('One-shot at date/time')}</option>
              </select>
            </div>
            <div className="ga-sched-field">
              <label>{t('Detail')}</label>
              <input
                className="ga-input"
                value={form.extra}
                onChange={(e) => setForm((f) => ({ ...f, extra: e.target.value }))}
                placeholder={t('e.g. 30 (minutes) or 03:00 (HH:MM)')}
              />
            </div>
            <div className="ga-sched-field">
              <label>{t('Action')}</label>
              <select
                className="ga-input"
                value={form.action}
                onChange={(e) => setForm((f) => ({ ...f, action: e.target.value as ActionKind }))}
              >
                <option value="extract_files">extract_files (knowledge)</option>
                <option value="notify">notify (UI message)</option>
              </select>
            </div>
            <div className="ga-sched-field" style={{ flex: 1, minWidth: 240 }}>
              <label>{t('Payload')}</label>
              <input
                className="ga-input"
                value={form.payload}
                onChange={(e) => setForm((f) => ({ ...f, payload: e.target.value }))}
                placeholder={t('extract: optional prompt — notify: message text')}
              />
            </div>
            <div className="ga-sched-field">
              <label>{t('Agent (optional)')}</label>
              <input
                className="ga-input"
                value={form.agent}
                onChange={(e) => setForm((f) => ({ ...f, agent: e.target.value }))}
              />
            </div>
            <div>
              <button type="submit" className="ga-btn ga-btn-primary" disabled={submitting}>
                {submitting ? t('Creating…') : t('Create')}
              </button>
            </div>
          </div>
          <p className="ga-sched-muted" style={{ margin: '6px 0 0 0' }}>
            {t(
              'Per-character actions (send_message, set_status, execute_tool) are not exposed here — they belong in the AgentLoop. Daily Rhythm: Character Editor → Daily schedule.',
            )}
          </p>
        </form>
      </section>
    </div>
  )
}
