import { expect, test } from '@playwright/test'

// Full journey against the running stack: sign in -> upload -> processing -> search -> open source.
const TOKEN = process.env.PAW_TOKEN ?? 'paw_local_only_owner_token'
const fact = `zebracrossing${Date.now()}`

test('upload a document, see it processed, find a known fact, open its source', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Owner access token').fill('definitely-not-the-token')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('alert')).toContainText('not accepted')

  await page.getByLabel('Owner access token').fill(TOKEN)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Documents', exact: true })).toBeVisible()

  const name = `journey-${Date.now()}.md`
  await page.locator('#file').setInputFiles({
    name,
    mimeType: 'text/markdown',
    buffer: Buffer.from(`# Field notes\nThe secret marker is ${fact} and it lives under the old bridge.\n`),
  })
  await expect(page.getByText(`${name} uploaded`)).toBeVisible()

  const row = page.getByRole('row').filter({ hasText: name })
  await expect(row.getByText('Ready')).toBeVisible({ timeout: 30_000 })

  // durable status survives a reload
  await page.reload()
  await expect(page.getByRole('row').filter({ hasText: name }).getByText('Ready')).toBeVisible()

  await page.getByRole('link', { name: 'Search' }).click()
  await page.getByLabel('Search your documents').fill(fact)
  await page.getByRole('button', { name: 'Search' }).click()
  const hit = page.getByRole('listitem').filter({ hasText: name }).first()
  await expect(hit).toContainText(fact)
  await expect(hit).toContainText('Field notes')

  await hit.getByRole('button', { name: 'Open source' }).click()
  const source = page.getByRole('complementary', { name: 'Source passage' })
  await expect(source).toContainText(name)
  await expect(source.locator('pre')).toContainText(fact)

  // no-match query is handled gracefully
  await page.getByLabel('Search your documents').fill('qqqzzzxxxnothing')
  await page.getByRole('button', { name: 'Search' }).click()
  await expect(page.getByText('No matching passages')).toBeVisible()
})
