import { defineConfig } from "vitest/config";

// Benchmark-only configuration (M-2). It is not part of `npm test`: the default config only picks up
// tests/. Run with: npx vitest run --config vitest.bench.config.ts
export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["../../scripts/bench/*.bench.ts"],
    pool: "forks",
    fileParallelism: false,
  },
});
