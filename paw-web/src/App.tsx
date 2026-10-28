import { AuthProvider, useAuth } from './auth'
import { DocumentsPage } from './components/DocumentsPage'
import { LoginForm } from './components/LoginForm'
import { SearchPage } from './components/SearchPage'
import { ServiceStatus } from './components/ServiceStatus'
import { Shell } from './components/Shell'
import { useHashRoute } from './hooks/useHashRoute'

function Routes() {
  const route = useHashRoute()
  const { signedIn } = useAuth()
  let page
  if (route === 'status') page = <ServiceStatus />
  else if (!signedIn) page = <LoginForm />
  else page = route === 'search' ? <SearchPage /> : <DocumentsPage />
  return <Shell route={route}>{page}</Shell>
}

export default function App() {
  return (
    <AuthProvider>
      <Routes />
    </AuthProvider>
  )
}
