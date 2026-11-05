import type { AgentSpend, RunCost } from '../api/client'
import { formatUsd } from '../lib/format'

/** What a run has cost so far: confirmed money apart from money held for a call whose bill is not yet known. */
export function RunCostLine({ cost }: { cost?: RunCost }) {
  if (!cost || cost.status === 'unknown') return null
  const held = Number(cost.reserved_usd ?? 0)
  return (
    <p className="muted small run-cost" aria-label="Run cost">
      Cost {formatUsd(cost.confirmed_usd)} confirmed
      {held > 0 && (
        <>
          {' '}
          · {formatUsd(cost.reserved_usd)} held
          {cost.status === 'unconfirmed' ? ' (may or may not have been billed)' : ' while a call is in flight'}
        </>
      )}
    </p>
  )
}

const percent = (spent: number, limit: number) => (limit > 0 ? Math.min(100, Math.round((spent / limit) * 100)) : 100)

/** Today's spending against the daily limits. Held money counts against the limit even though it is not billed yet. */
export function SpendPanel({ spend }: { spend: AgentSpend | null }) {
  if (!spend) return null
  const rows: [string, AgentSpend['actor']][] = [
    ['Admin Hermès today', spend.actor],
    ['All agents today', spend.shared],
  ]
  const resets = new Date(spend.resets_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return (
    <details className="spend-panel card">
      <summary>
        Spending today: {formatUsd(spend.actor.confirmed_usd)} confirmed of {formatUsd(spend.actor.limit_usd)}
        {Number(spend.actor.reserved_usd) > 0 && <> · {formatUsd(spend.actor.reserved_usd)} held</>}
      </summary>
      <dl>
        {rows.map(([label, v]) => {
          const used = Number(v.confirmed_usd) + Number(v.reserved_usd)
          const limit = Number(v.limit_usd)
          return (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                <meter min={0} max={limit} value={Math.min(used, limit)} aria-label={`${label} used`} />
                <span>
                  {' '}
                  {percent(used, limit)}% of {formatUsd(v.limit_usd)}
                </span>
                <span className="muted small">
                  {' '}
                  · {formatUsd(v.confirmed_usd)} confirmed, {formatUsd(v.reserved_usd)} held
                  {Number(v.unconfirmed_usd) > 0 ? `, of which ${formatUsd(v.unconfirmed_usd)} may or may not have been billed` : ''}
                </span>
              </dd>
            </div>
          )
        })}
      </dl>
      <p className="muted small">
        Held money counts against the limit until the provider&apos;s usage is known. Limits reset at {resets}, at midnight India time.
      </p>
    </details>
  )
}
