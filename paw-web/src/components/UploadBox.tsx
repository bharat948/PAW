import { useRef, useState } from 'react'
import { ApiError, uploadDocument } from '../api/client'

const ACCEPT = '.txt,.md,.markdown,.pdf,.csv,.tsv,.json,.jsonl'

export function UploadBox({ onUploaded }: { onUploaded: () => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [progress, setProgress] = useState<number | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [drag, setDrag] = useState(false)

  async function send(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      setMsg(null)
      setProgress(0)
      try {
        const r = await uploadDocument(file, setProgress)
        setMsg({
          kind: 'ok',
          text: r.duplicate
            ? `${file.name} was already uploaded; showing the existing document.`
            : `${file.name} uploaded. Processing continues in the background.`,
        })
        onUploaded()
      } catch (e) {
        const text = e instanceof ApiError ? e.message : 'Upload failed.'
        setMsg({ kind: 'err', text: `${file.name}: ${text}` })
      } finally {
        setProgress(null)
      }
    }
    if (input.current) input.current.value = ''
  }

  return (
    <section
      className={`card dropzone${drag ? ' drag' : ''}`}
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
      <h2 id="up-h">Upload documents</h2>
      <p className="muted">
        Text, Markdown, text-based PDF, CSV/TSV, JSON/JSONL. Up to 10 MiB each. Drop files here or choose them.
      </p>
      <label htmlFor="file" className="btn">
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
      {progress !== null && (
        <progress value={progress} max={100} aria-label="Upload progress">
          {progress}%
        </progress>
      )}
      <div aria-live="polite">
        {msg && <p className={msg.kind === 'ok' ? 'ok-text' : 'error-text'}>{msg.text}</p>}
      </div>
    </section>
  )
}
