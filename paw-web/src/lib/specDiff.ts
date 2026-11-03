import type { ProfileSpec } from '../api/client'

export interface DiffRow {
  field: string
  before: string
  after: string
}

const LABELS: Record<string, string> = {
  'persona.title': 'Title',
  'persona.summary': 'Summary',
  'persona.expertise': 'Expertise',
  'persona.communication.style': 'Style',
  'persona.communication.uncertainty': 'Uncertainty',
  ethos: 'Rules',
  'runtime.model': 'Model',
  'runtime.max_iterations': 'Max steps',
  'runtime.max_run_seconds': 'Max run seconds',
  'tools.grants': 'Tools',
  'budgets.per_run_usd': 'Per-run budget (USD)',
  'budgets.daily_usd': 'Daily budget (USD)',
}

function flatten(spec: ProfileSpec): Record<string, string> {
  return {
    'persona.title': spec.persona.title,
    'persona.summary': spec.persona.summary,
    'persona.expertise': spec.persona.expertise.join(', '),
    'persona.communication.style': spec.persona.communication.style,
    'persona.communication.uncertainty': spec.persona.communication.uncertainty,
    ethos: spec.ethos.join('\n'),
    'runtime.model': spec.runtime.model,
    'runtime.max_iterations': String(spec.runtime.max_iterations),
    'runtime.max_run_seconds': String(spec.runtime.max_run_seconds),
    'tools.grants': [...spec.tools.grants].sort().join(', '),
    'budgets.per_run_usd': String(spec.budgets.per_run_usd),
    'budgets.daily_usd': String(spec.budgets.daily_usd),
  }
}

/** Field-by-field differences, in the order a person reads the profile. */
export function diffSpecs(before: ProfileSpec, after: ProfileSpec): DiffRow[] {
  const a = flatten(before)
  const b = flatten(after)
  return Object.keys(LABELS)
    .filter((k) => a[k] !== b[k])
    .map((k) => ({ field: LABELS[k], before: a[k] || '—', after: b[k] || '—' }))
}
