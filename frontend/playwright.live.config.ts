import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "live-financial-insights.spec.ts",
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  reporter: "list",
  outputDir: process.env.PFP_E2E_ARTIFACT_DIR ?? "./test-results/live-e2e",
  use: {
    baseURL: process.env.PFP_E2E_WEB_ORIGIN ?? "http://127.0.0.1:5173",
    ...devices["Desktop Chrome"],
    trace: "off",
    screenshot: "only-on-failure",
    video: "off",
  },
});
