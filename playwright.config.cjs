const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.spec.cjs',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 2,
  timeout: 20000,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    viewport: { width: 1366, height: 650 },
    trace: 'retain-on-failure',
    launchOptions: process.env.ARGOS_TEST_CHROMIUM ? { executablePath: process.env.ARGOS_TEST_CHROMIUM } : {},
  },
  webServer: {
    command: 'node tests/browser/server.cjs',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 10000,
  },
});
