import { useRef, useState } from 'react'
import { ApiError, uploadDocument } from '../api/client'
import { fileKind, formatBytes } from '../lib/format'
import { IconAlert, IconCheck, IconUpload } from './icons'

const ACCEPT = '.txt,.md,.markdown,.pdf,.csv,.tsv,.json,.jsonl'
const FORMATS = ['TXT', 'MD', 'PDF', 'CSV', 'TSV', 'JSON', 'JSONL']

interface Item {
  id: number
  name: string
  size: number
  progress: number
  state: 'uploading' | 'done' | 'duplicate' | 'error'
  message?: string
}

let nextId = 1

export function UploadBox({ onUploaded }: { onUploaded: () => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [items, setItems] = useState<Item[]>([])
  const [drag, setDrag] = useState(false)
  const [announce, setAnnounce] = useState('')

  const patch = (id: number, p: Partial<Item>) => setItems((xs) => xs.map((x) => (x.id === id ? { ...x, ...p } : x)))

  async function send(files: FileList | File[]) {
    const list = Array.from(files)
    const added: Item[] = list.map((f) => ({ id: nextId++, name: f.name, size: f.size, progress: 0, state: 'uploading' }))
    setItems((xs) => [...added, ...xs])
    for (const [i, file] of list.entries()) {
      const id = added[i].id
      try {
        const r = await uploadDocument(file, (pct) => patch(id, { progress: pct }))
        patch(id, { state: r.duplicate ? 'duplicate' : 'done', progress: 100 })
        setAnnounce(
          r.duplicate
            ? `${file.name} was already uploaded; showing the existing document.`
            : `${file.name} uploaded. Processing continues in the background.`,
        )
        onUploaded()
      } catch (e) {
        const message = e instanceof ApiError ? e.message : 'Upload failed.'
        patch(id, { state: 'error', message })
        setAnnounce(`${file.name}: ${message}`)
      }
    }
    if (input.current) input.current.value = ''
  }

  const finished = items.some((i) => i.state !== 'uploading')

  return (
    <section
      className={`dropzone${drag ? ' drag' : ''}`}
      aria-labelledby="up-h"
      onDragOver={(e) => {
        e.preventDefault()
        setDrag(true)
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDrag(false)
        void send(e.dataTransfer.files)
      }}
    >
      <span className="dz-icon">
        <IconUpload size={26} />
      </span>
      <h2 id="up-h">Upload documents</h2>
      <p className="muted">Drag files here, or choose them. Up to 10 MiB each.</p>
      <label htmlFor="file" className="btn primary">
        Choose files
      </label>
      <input
        id="file"
        ref={input}
        type="file"
        accept={ACCEPT}
        multiple
        className="visually-hidden"
        onChange={(e) => e.target.files && void send(e.target.files)}
      />
      <div className="chips" aria-label="Supported formats">
        {FORMATS.map((f) => (
          <span key={f} className="tag">
            {f}
          </span>
        ))}
      </div>

      {items.length > 0 && (
        <ul className="queue" style={{ width: '100%' }} aria-label="Upload queue">
          {items.slice(0, 6).map((it) => {
            const k = fileKind(it.name)
            return (
              <li key={it.id}>
                <span className={`ftype kind-${k.kind}`} style={{ width: 34, height: 34 }}>
                  {k.label}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="qname">{it.name}</div>
                  {it.state === 'uploading' ? (
                    <div className="bar">
                      <progress className="visually-hidden" value={it.progress} max={100} aria-label={`Uploading ${it.name}`} />
                      <i style={{ width: `${it.progress}%` }} />
                    </div>
                  ) : (
                    <div className={`small ${it.state === 'error' ? 'error-text' : 'muted'}`}>
                      {it.state === 'error'
                        ? it.message
                        : it.state === 'duplicate'
                          ? 'Already uploaded'
                          : 'Stored. Indexing in the background'}
                    </div>
                  )}
                </div>
                <span className="small muted" style={{ display: 'inline-flex', gap: '.35rem', alignItems: 'center' }}>
                  {it.state === 'done' && <IconCheck size={16} />}
                  {it.state === 'error' && <IconAlert size={16} />}
                  {formatBytes(it.size)}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      {finished && (
        <button type="button" className="ghost" onClick={() => setItems((xs) => xs.filter((x) => x.state === 'uploading'))}>
          Clear finished
        </button>
      )}
      <div className="visually-hidden" aria-live="polite">
        {announce}
      </div>
    </section>
  )
}
