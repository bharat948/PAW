import type { DocStatus } from '../api/client'

const LABEL: Record<DocStatus, string> = {
  RECEIVED: 'Received', STORED: 'Queued', EXTRACTING: 'Extracting', CHUNKING: 'Chunking',
  EMBEDDING: 'Embedding', INDEXING: 'Indexing', COMPLETED: 'Ready', FAILED: 'Failed',
  UNSUPPORTED: 'Unsupported',
}

export function StatusBadge({ status }: { status: DocStatus }) {
  const kind = status === 'COMPLETED' ? 'ok' : status === 'FAILED' ? 'bad' : status === 'UNSUPPORTED' ? 'warn' : 'busy'
  return <span className={`pill ${kind}`}>{LABEL[status]}</span>
}
