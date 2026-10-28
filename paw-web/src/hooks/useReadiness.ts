import { useCallback, useEffect, useState } from 'react'
import { ApiError, getReady } from '../api/client'

export type ReadinessState =
  | { phase: 'loading' }
  | { phase: 'ready' }
  | { phase: 'unavailable'; failedCheck?: string; correlationId?: string; unreachable: boolean }

export function useReadiness(): { state: ReadinessState; refresh: () => void } {
  const [state, setState] = useState<ReadinessState>({ phase: 'loading' })
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const ctl = new AbortController()
    getReady()
      .then(() => !ctl.signal.aborted && setState({ phase: 'ready' }))
      .catch((e: unknown) => {
        if (ctl.signal.aborted) return
        const err = e instanceof ApiError ? e : undefined
        const check = err?.envelope?.details?.[0]?.check
        setState({
          phase: 'unavailable',
          unreachable: !err || err.status === 0,
          failedCheck: typeof check === 'string' ? check : undefined,
          correlationId: err?.envelope?.correlation_id,
        })
      })
    return () => ctl.abort()
  }, [tick])

  const refresh = useCallback(() => {
    setState({ phase: 'loading' })
    setTick((t) => t + 1)
  }, [])
  return { state, refresh }
}
