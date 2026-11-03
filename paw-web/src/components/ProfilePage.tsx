import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  ApiError,
  activateProfileVersion,
  createProfileDraft,
  getProfile,
  stepUpUrl,
  type DraftResult,
  type ProfileSpec,
  type ProfileVersion,
  type ProfileView,
} from '../api/client'
import { diffSpecs } from '../lib/specDiff'
import { ErrorState } from './ErrorState'
import { Loading } from './Loading'

const CHANGE_LABEL: Record<string, string> = { tools: 'the tools it may use', budgets: 'its budgets', runtime: 'its model or run limits' }
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '')

function saveError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 422) return e.envelope?.code === 'unknown_tool' ? e.message : 'Some values are not valid. Check the fields and try again.'
  }
  return 'Could not save. Please try again.'
}

export function ProfilePage() {
  const [view, setView] = useState<ProfileView | null>(null)
  const [failed, setFailed] = useState(false)
  const [form, setForm] = useState<ProfileSpec | null>(null)
  const [draft, setDraft] = useState<DraftResult | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [needStepUp, setNeedStepUp] = useState(false)
  const [busy, setBusy] = useState(false)
  const [compare, setCompare] = useState<ProfileVersion | null>(null)

  const load = useCallback(() => {
    setFailed(false)
    getProfile()
      .then((v) => {
        setView(v)
        setForm(structuredClone(v.active?.spec ?? null))
      })
      .catch(() => setFailed(true))
  }, [])
  useEffect(load, [load])

  if (failed) return <ErrorState title="Could not load the profile" onRetry={load} />
  if (!view || !form) return <Loading label="Loading profile…" />
  const active = view.active

  const set = (f: (s: ProfileSpec) => void) => {
    const next = structuredClone(form)
    f(next)
    setForm(next)
    setDraft(null)
    setNotice(null)
  }
  const num = (v: string) => (v.trim() === '' || Number.isNaN(Number(v)) ? 0 : Number(v))

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!form) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      setDraft(await createProfileDraft(form))
      setConfirm(false)
    } catch (err) {
      setError(saveError(err))
    } finally {
      setBusy(false)
    }
  }

  async function activate() {
    if (!draft) return
    setBusy(true)
    setError(null)
    setNeedStepUp(false)
    try {
      const r = await activateProfileVersion(draft.id, confirm)
      setNotice(
        `Version ${r.version} is now active.` +
          (r.restart_required ? ' Restart the Hermès container to apply the model or tool changes (./dev.sh restarts it).' : ' New messages use it right away.'),
      )
      setDraft(null)
      load()
    } catch (err) {
      if (err instanceof ApiError && err.envelope?.code === 'stepup_required') setNeedStepUp(true)
      else if (err instanceof ApiError && err.envelope?.code === 'confirmation_required') setError('Please tick the confirmation first.')
      else setError('Could not activate this version. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  const toggleTool = (t: string, on: boolean) =>
    set((s) => {
      const g = new Set(s.tools.grants)
      if (on) g.add(t)
      else g.delete(t)
      s.tools.grants = [...g].sort()
    })

  return (
    <section aria-labelledby="profile-h">
      <h1 id="profile-h">Admin Hermès profile</h1>
      <p className="muted">
        Who Admin Hermès is and what it may do. Changes create a new version; earlier versions are kept, and each run records the version it used.
      </p>
      {view.restart_required && (
        <p role="alert" className="error-text">
          The Hermès container is running an older tool list or model than the active profile. Restart it (./dev.sh) to apply it;
          until then its tool calls are refused.
        </p>
      )}
      {notice && <p role="status" className="ok-text">{notice}</p>}
      {error && <p role="alert" className="error-text">{error}</p>}
      {needStepUp && (
        <p role="alert" className="error-text">
          Please sign in again to confirm this change. <a href={stepUpUrl('/#/profile')}>Sign in again</a>
        </p>
      )}

      <form className="card profile-form" onSubmit={save} aria-label="Edit profile">
        <p className="muted">Editing a copy of version {active?.version}. Nothing changes until you activate the new version.</p>
        <label htmlFor="p-title">Title</label>
        <input id="p-title" value={form.persona.title} maxLength={80} onChange={(e) => set((s) => (s.persona.title = e.target.value))} />
        <label htmlFor="p-summary">Summary</label>
        <textarea id="p-summary" rows={3} value={form.persona.summary} maxLength={600} onChange={(e) => set((s) => (s.persona.summary = e.target.value))} />
        <label htmlFor="p-ethos">Rules (one per line)</label>
        <textarea id="p-ethos" rows={5} value={form.ethos.join('\n')} onChange={(e) => set((s) => (s.ethos = e.target.value.split('\n').filter((l) => l.trim())))} />
        <label htmlFor="p-style">Style</label>
        <select id="p-style" value={form.persona.communication.style} onChange={(e) => set((s) => (s.persona.communication.style = e.target.value as ProfileSpec['persona']['communication']['style']))}>
          <option value="concise">Concise</option>
          <option value="balanced">Balanced</option>
          <option value="detailed">Detailed</option>
        </select>

        <fieldset>
          <legend>Tools it may use</legend>
          {view.known_tools.map((t) => (
            <label key={t} className="check">
              <input type="checkbox" checked={form.tools.grants.includes(t)} onChange={(e) => toggleTool(t, e.target.checked)} />
              <code>{t}</code>
            </label>
          ))}
        </fieldset>

        <div className="grid-2">
          <div>
            <label htmlFor="p-model">Model</label>
            <input id="p-model" value={form.runtime.model} onChange={(e) => set((s) => (s.runtime.model = e.target.value))} />
          </div>
          <div>
            <label htmlFor="p-iter">Max steps per run</label>
            <input id="p-iter" inputMode="numeric" value={form.runtime.max_iterations} onChange={(e) => set((s) => (s.runtime.max_iterations = num(e.target.value)))} />
          </div>
          <div>
            <label htmlFor="p-perrun">Budget per run (USD)</label>
            <input id="p-perrun" inputMode="decimal" value={form.budgets.per_run_usd} onChange={(e) => set((s) => (s.budgets.per_run_usd = num(e.target.value)))} />
          </div>
          <div>
            <label htmlFor="p-daily">Budget per day (USD)</label>
            <input id="p-daily" inputMode="decimal" value={form.budgets.daily_usd} onChange={(e) => set((s) => (s.budgets.daily_usd = num(e.target.value)))} />
          </div>
        </div>
        <button type="submit" disabled={busy}>
          {busy && !draft ? 'Saving…' : 'Save as new version'}
        </button>
      </form>

      {draft && (
        <div className="card" role="region" aria-label="New version">
          <h2>Version {draft.version} saved (not active yet)</h2>
          {active && (
            <ul aria-label="What changes">
              {diffSpecs(active.spec, form).map((d) => (
                <li key={d.field}>
                  <strong>{d.field}</strong>: {d.before} → {d.after}
                </li>
              ))}
              {diffSpecs(active.spec, form).length === 0 && <li>No differences from the active version.</li>}
            </ul>
          )}
          {draft.requires_confirmation && (
            <label className="check">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
              I understand this changes {draft.changes.map((c) => CHANGE_LABEL[c] ?? c).join(' and ')}.
            </label>
          )}
          <button type="button" className="primary" onClick={activate} disabled={busy || (draft.requires_confirmation && !confirm)}>
            {busy ? 'Activating…' : `Activate version ${draft.version}`}
          </button>
        </div>
      )}

      <h2>Versions</h2>
      <ul className="version-list" aria-label="Versions">
        {view.versions.map((v) => (
          <li key={v.id} className="card">
            <span>
              <strong>Version {v.version}</strong> <span className={`pill ${v.status === 'active' ? 'ok' : ''}`}>{v.status}</span>
            </span>
            <span className="muted small">
              {v.status === 'draft' ? `drafted ${when(v.created_at)}` : `activated ${when(v.activated_at)}`}
            </span>
            {active && v.id !== active.id && (
              <button type="button" className="link-btn" onClick={() => setCompare(compare?.id === v.id ? null : v)}>
                {compare?.id === v.id ? 'Hide differences' : 'Compare with active'}
              </button>
            )}
            {compare?.id === v.id && active && (
              <ul aria-label={`Version ${v.version} compared with active`}>
                {diffSpecs(active.spec, v.spec).map((d) => (
                  <li key={d.field}>
                    <strong>{d.field}</strong>: active {d.before} · this version {d.after}
                  </li>
                ))}
                {diffSpecs(active.spec, v.spec).length === 0 && <li>Identical to the active version.</li>}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
