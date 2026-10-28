import { useEffect, useState } from 'react'

export type Route = 'documents' | 'search' | 'status'
const ROUTES: Route[] = ['documents', 'search', 'status']

const parse = (): Route => {
  const r = window.location.hash.replace(/^#\/?/, '') as Route
  return ROUTES.includes(r) ? r : 'documents'
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(parse)
  useEffect(() => {
    const on = () => setRoute(parse())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return route
}
