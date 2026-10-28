import { useState, type FormEvent } from 'react'
import { ApiError, getToken, listDocuments, setToken } from '../api/client'
import { useAuth } from '../auth'

export function LoginForm() {
  const { signIn } = useAuth()
  const [token, setTok] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

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
    <form onSubmit={submit} className="card" aria-labelledby="login-h">
      <h1 id="login-h">Sign in</h1>
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
      {error && (
        <p id="login-err" role="alert" className="error-text">
          {error}
        </p>
      )}
      <button type="submit" disabled={busy || !token.trim()}>
        {busy ? 'Checking…' : 'Sign in'}
      </button>
    </form>
  )
}
