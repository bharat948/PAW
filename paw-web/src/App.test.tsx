import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => vi.restoreAllMocks())

it('shows ready state', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ status: 'ready' }))
  window.location.hash = '#/status'
  render(<App />)
  expect(screen.getByRole('status')).toHaveTextContent(/checking/i)
  expect(await screen.findByText(/Database: ready/)).toBeInTheDocument()
})

it('shows a safe unavailable state on 503 and retries', async () => {
  const env = {
    code: 'not_ready', message: 'x', correlation_id: 'abc123', retryable: true,
    details: [{ check: 'database', status: 'failing' }],
  }
  const spy = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(Response.json(env, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ status: 'ready' }))
  window.location.hash = '#/status'
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/database is not ready/i)
  expect(screen.getByText(/abc123/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /try again/i }))
  expect(await screen.findByText(/Backend: ready/)).toBeInTheDocument()
  expect(spy).toHaveBeenCalledTimes(2)
})

it('shows unreachable state when the network fails', async () => {
  vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('boom'))
  window.location.hash = '#/status'
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveTextContent(/could not be reached/i)
})
