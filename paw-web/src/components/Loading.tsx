export function Loading({ label }: { label: string }) {
  return (
    <p className="loading" role="status">
      <span className="spinner" aria-hidden="true" /> {label}
    </p>
  )
}
