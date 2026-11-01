import { expect, test } from '@playwright/test'

// Upload a document, then ask about it: the answer must cite it and the trace must show where it came from.
const TOKEN = process.env.PAW_TOKEN ?? 'paw_local_only_owner_token'
const fact = `quokka${Date.now()}`

test('ask a question: cited answer, retrieval trace, source opens', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Owner access token').fill(TOKEN)
  await page.getByRole('button', { name: 'Sign in' }).click()
  const name = `chat-${Date.now()}.md`
  await page.locator('#file').setInputFiles({
    name, mimeType: 'text/markdown',
    buffer: Buffer.from(`# Zoo notes\nThe ${fact} enclosure opens at nine and closes at five every day.\n`),
  })
  await expect(page.getByRole('row').filter({ hasText: name }).getByText('Ready', { exact: true })).toBeVisible({ timeout: 30_000 })

  await page.getByRole('link', { name: 'Ask' }).click()
  await page.getByLabel('Ask a question').fill(`When does the ${fact} enclosure open?`)
  await page.getByRole('button', { name: /send/i }).click()

  const cite = page.getByRole('button', { name: 'Source 1' }).first()
  await expect(cite).toBeVisible({ timeout: 20_000 })

  const trace = page.locator('details.trace').first()
  await trace.locator('summary').click()
  await expect(trace).toContainText(name)
  await expect(trace).toContainText('Search query used')

  await cite.click()
  const source = page.getByRole('complementary', { name: 'Source passage' })
  await expect(source).toContainText(name)
  await expect(source.locator('pre')).toContainText(fact)

  await page.keyboard.press('Escape') // dismiss the source panel
  await expect(source).toBeHidden()

  // unrelated question: honest "nothing found", the model is not called
  await page.getByLabel('Ask a question').fill('zzzqqq xylophone nonexistentterm')
  await page.getByRole('button', { name: /send/i }).click()
  await expect(page.getByText(/couldn't find anything relevant/)).toBeVisible({ timeout: 20_000 })
})
