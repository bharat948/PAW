import type { DocStatus } from '../api/client'

const STEPS = ['Queued', 'Extract', 'Chunk', 'Embed', 'Index']
const POS: Partial<Record<DocStatus, number>> = {
  RECEIVED: 0, STORED: 0, EXTRACTING: 1, CHUNKING: 2, EMBEDDING: 3, INDEXING: 4, COMPLETED: 5,
}

/** Five-segment progress through the ingestion pipeline (decorative; the status pill carries the meaning). */
export function PipelineProgress({ status }: { status: DocStatus }) {
  const pos = POS[status]
  if (pos === undefined) return null // FAILED / UNSUPPORTED: no progress to show
  return (
    <>
      <div className="steps" aria-hidden="true">
        {STEPS.map((s, i) => (
          <i key={s} className={i < pos ? 'done' : i === pos ? 'active' : ''} title={s} />
        ))}
      </div>
      {pos < 5 && <div className="stage-line">Step {pos + 1} of 5 · {STEPS[pos]}</div>}
    </>
  )
}
