import { expect, test } from '@playwright/test'

const READY = '**/ready'

test('shows ready state', async ({ page }) => {
  await page.route(READY, (r) => r.fulfill({ json: { status: 'ready' } }))
  await page.goto('/')
  await expect(page.getByText('Database: ready')).toBeVisible()
})

test('shows a safe service-unavailable state on 503', async ({ page }) => {
  await page.route(READY, (r) =>
    r.fulfill({
      status: 503,
      json: {
        code: 'not_ready', message: 'internal detail', correlation_id: 'cid-1', retryable: true,
        details: [{ check: 'database', status: 'failing' }],
      },
    }),
  )
  await page.goto('/')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Service unavailable')
  await expect(alert).not.toContainText('internal detail')
  // no horizontal scroll at this viewport
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  )
  expect(overflow).toBe(false)
})
