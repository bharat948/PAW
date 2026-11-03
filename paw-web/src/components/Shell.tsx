import type { ReactNode } from 'react'
import { useAuth } from '../auth'
import type { Route } from '../hooks/useHashRoute'
import { IconChat, IconFile, IconLogo, IconPlug, IconSearch } from './icons'

const LINKS: { route: Route; label: string; authed: boolean; icon: ReactNode }[] = [
  { route: 'documents', label: 'Documents', authed: true, icon: <IconFile size={16} /> },
  { route: 'search', label: 'Search', authed: true, icon: <IconSearch size={16} /> },
  { route: 'ask', label: 'Ask', authed: true, icon: <IconChat size={16} /> },
  { route: 'integrations', label: 'Integrations', authed: true, icon: <IconPlug size={16} /> },
  { route: 'status', label: 'Status', authed: false, icon: null },
]

export function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const { signedIn, signOut } = useAuth()
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <span className="brand">
          <span className="brand-mark">
            <IconLogo size={18} />
          </span>
          PAW
        </span>
        <nav aria-label="Primary">
          <ul>
            {LINKS.filter((l) => signedIn || !l.authed).map((l) => (
              <li key={l.route}>
                <a href={`#/${l.route}`} aria-current={route === l.route ? 'page' : undefined}>
                  {l.icon}
                  {l.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <span className="spacer" />
        {signedIn && (
          <button type="button" className="ghost" onClick={signOut}>
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
