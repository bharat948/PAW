import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, getAuthConfig, getToken, googleSignInUrl, listDocuments, setToken, type AuthConfig } from '../api/client'
import { useAuth } from '../auth'
import { IconLogo } from './icons'

const AUTH_ERRORS: Record<string, string> = {
  not_allowed: 'That Google account is not allowed to sign in to PAW.',
  failed: 'Google sign-in could not be verified. Please try again.',
  expired: 'That sign-in link expired. Please try again.',
  cancelled: 'Sign-in was cancelled.',
}

/** Reads (and removes) ?auth_error=... that the server appends when it sends the browser back after Google. */
function takeAuthError(): string | null {
  try {
    const url = new URL(window.location.href)
    const code = url.searchParams.get('auth_error')
    if (!code) return null
    url.searchParams.delete('auth_error')
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
    return AUTH_ERRORS[code] ?? 'Sign-in failed. Please try again.'
  } catch {
    return null
  }
}

export function LoginForm() {
  const { signIn } = useAuth()
  const [token, setTok] = useState('')
  const [error, setError] = useState<string | null>(takeAuthError)
  const [busy, setBusy] = useState(false)
  // null = unknown or unreachable: fall back to the original token form so the dev flow never breaks
  const [cfg, setCfg] = useState<AuthConfig | null>(null)

  useEffect(() => {
    let alive = true
    getAuthConfig()
      .then((c) => alive && typeof c?.mode === 'string' && setCfg(c))
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [])

  const showGoogle = !!cfg?.google
  // Google is the only sign-in once it is configured; the token form exists for token-only dev mode
  const showToken = !showGoogle

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const prev = getToken()
    setToken(token.trim())
    try {
      await listDocuments() // cheapest authenticated call: validates the token
      signIn(token.trim())
    } catch (err) {
      setToken(prev)
      setError(
        err instanceof ApiError && err.status === 401
          ? 'That token was not accepted.'
          : 'Could not reach the PAW service. Please try again.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-wrap">
      <div className="card auth-card">
        <span className="brand-mark">
          <IconLogo size={26} />
        </span>
        <h1 id="login-h">Sign in</h1>
        <p className="muted">Upload documents, watch them get indexed, and search them with sources.</p>
        {showGoogle && (
          <a className="btn primary" href={googleSignInUrl(`/${window.location.hash}`)}>
            Continue with Google
          </a>
        )}
        {showToken && (
          <form onSubmit={submit} aria-labelledby="login-h">
            <label htmlFor="token">Owner access token</label>
            <input
              id="token"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setTok(e.target.value)}
              required
              aria-describedby={error ? 'login-err' : undefined}
            />
            <button type="submit" className="primary" disabled={busy || !token.trim()}>
              {busy ? 'Checking…' : 'Sign in'}
            </button>
          </form>
        )}
        {error && (
          <p id="login-err" role="alert" className="error-text">
            {error}
          </p>
        )}
      </div>
    </div>
  )
}
