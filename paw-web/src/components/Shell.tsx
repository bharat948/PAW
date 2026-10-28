import type { ReactNode } from 'react'

export function Shell({ children }: { children: ReactNode }) {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <span className="brand">PAW</span>
        <nav aria-label="Primary">
          <ul>
            <li>
              <a href="#main" aria-current="page">
                Status
              </a>
            </li>
          </ul>
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        {children}
      </main>
    </>
  )
}
