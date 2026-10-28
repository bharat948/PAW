import type { ReactNode } from 'react'

interface Props {
  title: string
  children?: ReactNode
  onRetry?: () => void
}

/** User-safe error panel. Never render server internals here. */
export function ErrorState({ title, children, onRetry }: Props) {
  return (
    <div className="error-state" role="alert">
      <h2>{title}</h2>
      {children}
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}
