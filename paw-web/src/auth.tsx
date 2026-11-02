import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { UNAUTHORIZED_EVENT, getMeQuiet, getToken, logout, setCsrf, setToken } from './api/client'

interface Auth {
  signedIn: boolean
  /** True only while a cookie-session probe is in flight, so the login screen does not flash. */
  loading: boolean
  email: string | null
  signIn: (token: string) => void
  signOut: () => void
}
const Ctx = createContext<Auth | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(() => !!getToken())
  const [loading, setLoading] = useState(() => !getToken())
  const [email, setEmail] = useState<string | null>(null)
  const [csrf, setCsrfState] = useState<string | null>(null)

  // No dev token stored: ask the server whether a Google session cookie is already valid.
  useEffect(() => {
    if (getToken()) return
    let alive = true
    getMeQuiet()
      .then((me) => {
        if (!alive || me.mode !== 'session') return
        setCsrf(me.csrf_token)
        setCsrfState(me.csrf_token)
        setEmail(me.email)
        setSignedIn(true)
      })
      .catch(() => undefined) // 401 or unreachable: simply not signed in
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const signIn = useCallback((t: string) => {
    setToken(t)
    setSignedIn(true)
  }, [])
  const signOut = useCallback(() => {
    setToken(null)
    setSignedIn(false)
    setEmail(null)
    setCsrf(null)
    if (csrf) logout(csrf).catch(() => undefined) // revoke server-side; the cookie is dead either way
    setCsrfState(null)
  }, [csrf])
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, signOut)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, signOut)
  }, [signOut])
  return <Ctx.Provider value={{ signedIn, loading, email, signIn, signOut }}>{children}</Ctx.Provider>
}

export function useAuth(): Auth {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth outside AuthProvider')
  return v
}
