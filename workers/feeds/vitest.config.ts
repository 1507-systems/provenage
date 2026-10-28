import { defineConfig } from 'vitest/config';

// Tests use a fake R2 binding and call the handler directly, so plain Node
// is enough. To run inside workerd instead, swap in
// @cloudflare/vitest-pool-workers (defineWorkersConfig) -- the tests are
// written against the standard Request/Response APIs and need no changes.
export default defineConfig({ test: { include: ['test/**/*.test.ts'] } });
