import { defineConfig } from '@playwright/test';

// Validation only: serves site/ on localhost and drives it in Chromium (and WebKit for the focus tests). No network beyond localhost;
// requests to the real API host are intercepted and answered from fixtures inside the tests.
export default defineConfig({
  testDir: 'test/e2e',
  timeout: 30000,
  retries: 0,
  reporter: [['list']],
  outputDir: 'test-results',
  use: { baseURL: 'http://127.0.0.1:4173' },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    // WebKit is the only engine on iOS and differs on focus scrolling, so it runs the focus tests too.
    { name: 'webkit', use: { browserName: 'webkit' }, grep: /SC 2\.4\.11/ },
  ],
  webServer: {
    command: 'node test/serve.mjs',
    url: 'http://127.0.0.1:4173/index.html',
    reuseExistingServer: !process.env.CI,
    timeout: 20000,
  },
});
