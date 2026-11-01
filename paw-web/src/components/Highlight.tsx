import { Fragment } from 'react'

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Wraps matches in <mark>. Text is always rendered as React text nodes (escaped), never as HTML. */
export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length) return <>{text}</>
  const parts = text.split(new RegExp(`(${terms.map(esc).join('|')})`, 'gi'))
  return (
    <>
      {parts.map((p, i) => (i % 2 === 1 ? <mark key={i}>{p}</mark> : <Fragment key={i}>{p}</Fragment>))}
    </>
  )
}
