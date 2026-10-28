import type { ReactNode } from 'react'
import { useAuth } from '../auth'
import type { Route } from '../hooks/useHashRoute'

const LINKS: { route: Route; label: string; authed: boolean }[] = [
  { route: 'documents', label: 'Documents', authed: true },
  { route: 'search', label: 'Search', authed: true },
  { route: 'status', label: 'Status', authed: false },
]

export function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const { signedIn, signOut } = useAuth()
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <span className="brand">PAW</span>
        <nav aria-label="Primary">
          <ul>
            {LINKS.filter((l) => signedIn || !l.authed).map((l) => (
              <li key={l.route}>
                <a href={`#/${l.route}`} aria-current={route === l.route ? 'page' : undefined}>
                  {l.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        {signedIn && (
          <button type="button" className="link-btn" onClick={signOut}>
            Sign out
          </button>
        )}
      </header>
      <main id="main" tabIndex={-1}>
        {children}
      </main>
    </>
  )
}
