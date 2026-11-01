import { Fragment } from 'react'

/** Renders answer text; `[n]` markers that match a real source become buttons. All text is escaped. */
export function CitedText({
  text, nSources, active, onCite,
}: { text: string; nSources: number; active: number | null; onCite: (n: number) => void }) {
  const parts = text.split(/(\[\d{1,2}\])/g)
  return (
    <>
      {parts.map((p, i) => {
        const m = /^\[(\d{1,2})\]$/.exec(p)
        const n = m ? Number(m[1]) : 0
        if (n >= 1 && n <= nSources)
          return (
            <button
              key={i} type="button" className={`cite${active === n ? ' on' : ''}`}
              aria-label={`Source ${n}`} onClick={() => onCite(n)}
            >
              {n}
            </button>
          )
        return <Fragment key={i}>{p}</Fragment>
      })}
    </>
  )
}
