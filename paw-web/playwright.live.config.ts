import { defineConfig, devices } from '@playwright/test'

// Runs against an already-running stack (`make up` in paw-server). No mocks.
export default defineConfig({
  testDir: './e2e-live',
  use: { baseURL: process.env.PAW_WEB_URL ?? 'http://localhost:8080' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
})
