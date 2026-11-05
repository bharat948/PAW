export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

/** A dollar amount from an exact decimal string. Never rounds a non-zero amount down to $0.00. */
export function formatUsd(amount: string | null | undefined): string {
  if (amount === null || amount === undefined) return 'unknown'
  const n = Number(amount)
  if (!Number.isFinite(n)) return 'unknown'
  if (n === 0) return '$0.00'
  if (n < 0.0001) return '<$0.0001'
  return '$' + n.toFixed(4).replace(/(\.\d\d)0+$/, '$1').replace(/(\.\d\d\d)0$/, '$1')
}

export function relativeTime(iso: string, now = Date.now()): string {
  const s = Math.round((now - new Date(iso).getTime()) / 1000)
  if (s < 45) return 'just now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.round(h / 24)
  return d < 30 ? `${d} d ago` : new Date(iso).toLocaleDateString()
}

export type FileKind = 'pdf' | 'md' | 'txt' | 'csv' | 'json' | 'other'

export function fileKind(name: string): { kind: FileKind; label: string } {
  const ext = name.toLowerCase().split('.').pop() ?? ''
  if (ext === 'pdf') return { kind: 'pdf', label: 'PDF' }
  if (ext === 'md' || ext === 'markdown') return { kind: 'md', label: 'MD' }
  if (ext === 'txt') return { kind: 'txt', label: 'TXT' }
  if (ext === 'csv' || ext === 'tsv') return { kind: 'csv', label: ext.toUpperCase() }
  if (ext === 'json' || ext === 'jsonl' || ext === 'ndjson') return { kind: 'json', label: 'JSON' }
  return { kind: 'other', label: ext.slice(0, 4).toUpperCase() || 'FILE' }
}

const STOP = new Set(['the', 'and', 'for', 'with', 'of', 'to', 'in', 'is', 'on', 'an', 'a', 'at', 'by', 'or'])

/** Lowercased distinct search words worth highlighting. */
export function queryTerms(q: string): string[] {
  const words = q.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? []
  return Array.from(new Set(words)).filter((w) => w.length > 1 && !STOP.has(w))
}
