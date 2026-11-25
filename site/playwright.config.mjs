import { defineConfig } from '@playwright/test';

// Validation only: serves site/ on localhost and drives it in Chromium. No network beyond localhost;
// requests to the real API host are intercepted and answered from fixtures inside the tests.
export default defineConfig({
  testDir: 'test/e2e',
  timeout: 30000,
  retries: 0,
  reporter: [['list']],
  outputDir: 'test-results',
  use: { baseURL: 'http://127.0.0.1:4173', browserName: 'chromium' },
  webServer: {
    command: 'node test/serve.mjs',
    url: 'http://127.0.0.1:4173/index.html',
    reuseExistingServer: !process.env.CI,
    timeout: 20000,
  },
});
