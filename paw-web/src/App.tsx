import { AuthProvider, useAuth } from './auth'
import { AgentPage } from './components/AgentPage'
import { ProfilePage } from './components/ProfilePage'
import { AskPage } from './components/AskPage'
import { DocumentsPage } from './components/DocumentsPage'
import { IntegrationsPage } from './components/IntegrationsPage'
import { LoginForm } from './components/LoginForm'
import { SearchPage } from './components/SearchPage'
import { ServiceStatus } from './components/ServiceStatus'
import { Shell } from './components/Shell'
import { useHashRoute } from './hooks/useHashRoute'

function Routes() {
  const route = useHashRoute()
  const { signedIn, loading } = useAuth()
  let page
  if (route === 'status') page = <ServiceStatus />
  else if (loading) page = <p role="status" className="muted">Checking your session…</p>
  else if (!signedIn) page = <LoginForm />
  else if (route === 'search') page = <SearchPage />
  else if (route === 'ask') page = <AskPage />
  else if (route === 'agent') page = <AgentPage />
  else if (route === 'profile') page = <ProfilePage />
  else if (route === 'integrations') page = <IntegrationsPage />
  else page = <DocumentsPage />
  return <Shell route={route}>{page}</Shell>
}

export default function App() {
  return (
    <AuthProvider>
      <Routes />
    </AuthProvider>
  )
}
