import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e-ledger',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:15174', trace: 'retain-on-failure' },
  webServer: [
    { command: '.venv/bin/python -m tests.sessions.browser_server', cwd: '../paw-server', url: 'http://127.0.0.1:18002/health', timeout: 60000, reuseExistingServer: false },
    { command: 'npm run dev -- --host 127.0.0.1 --port 15174 --strictPort', env: { VITE_API_BASE_URL: 'http://127.0.0.1:18002' }, url: 'http://127.0.0.1:15174', reuseExistingServer: false },
  ],
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
})
