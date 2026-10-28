import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { UNAUTHORIZED_EVENT, getToken, setToken } from './api/client'

interface Auth {
  signedIn: boolean
  signIn: (token: string) => void
  signOut: () => void
}
const Ctx = createContext<Auth | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(() => !!getToken())
  const signIn = useCallback((t: string) => {
    setToken(t)
    setSignedIn(true)
  }, [])
  const signOut = useCallback(() => {
    setToken(null)
    setSignedIn(false)
  }, [])
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, signOut)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, signOut)
  }, [signOut])
  return <Ctx.Provider value={{ signedIn, signIn, signOut }}>{children}</Ctx.Provider>
}

export function useAuth(): Auth {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth outside AuthProvider')
  return v
}
