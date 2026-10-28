import { expect, test } from '@playwright/test'

// Set PAW_EXPECT=down when the database has been stopped.
const down = process.env.PAW_EXPECT === 'down'

test(down ? 'db down shows safe unavailable state' : 'stack is ready end to end', async ({ page }) => {
  await page.goto('/')
  if (down) {
    await expect(page.getByRole('alert')).toContainText('database is not ready')
    await expect(page.getByRole('alert')).toContainText('Reference:')
  } else {
    await expect(page.getByText('Backend: ready')).toBeVisible()
    await expect(page.getByText('Database: ready')).toBeVisible()
  }
})
