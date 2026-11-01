import type { ReactNode } from 'react'
import { IconAlert } from './icons'

interface Props {
  title: string
  children?: ReactNode
  onRetry?: () => void
}

/** User-safe error panel. Never render server internals here. */
export function ErrorState({ title, children, onRetry }: Props) {
  return (
    <div className="card error-state" role="alert" style={{ borderColor: 'var(--bad)' }}>
      <h2 style={{ display: 'flex', gap: '.5rem', alignItems: 'center', color: 'var(--bad)' }}>
        <IconAlert size={20} /> {title}
      </h2>
      <div style={{ marginTop: '.5rem' }}>{children}</div>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}
