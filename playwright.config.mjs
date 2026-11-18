import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  outputDir: ".verification/browser-results",
  reporter: [["list"], ["json", { outputFile: ".verification/m3/playwright-results.json" }]],
  fullyParallel: false,
  workers: 1,
  use: { baseURL: "http://127.0.0.1:8814", browserName: "chromium", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "wide", use: { viewport: { width: 1920, height: 1080 } } },
    { name: "tablet", use: { viewport: { width: 834, height: 1112 }, hasTouch: true } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: "narrow", use: { viewport: { width: 320, height: 740 }, isMobile: true, hasTouch: true } },
  ],
  webServer: { command: "python -B tests/preview_server.py", url: "http://127.0.0.1:8814/preview/", reuseExistingServer: true },
});
