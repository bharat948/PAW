export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="stack" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: i === 0 ? '1.2em' : '.9em', width: `${95 - i * 12}%` }} />
      ))}
    </div>
  )
}
