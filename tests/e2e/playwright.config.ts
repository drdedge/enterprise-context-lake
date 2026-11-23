import path from "path";
import { defineConfig, devices } from "@playwright/test";

const repoRoot = path.resolve(__dirname, "..", "..");
const externalServers = process.env.ECL_EXTERNAL_SERVERS === "1";
const webUrl = process.env.ECL_WEB_URL || "http://127.0.0.1:5173";
const apiUrl = process.env.ECL_API_URL || "http://127.0.0.1:8000";

export default defineConfig({
  testDir: __dirname,
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  reporter: [["list"]],
  use: {
    baseURL: webUrl,
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
  webServer: externalServers
    ? undefined
    : [
        {
          command: `"${path.join(repoRoot, ".venv", "Scripts", "python.exe")}" "${path.join(repoRoot, "scripts", "dev.py")}" --llm-mode deterministic --api-port 8000 --web-port 5173`,
          url: `${apiUrl}/healthz`,
          reuseExistingServer: false,
          timeout: 60_000,
          cwd: repoRoot,
        },
      ],
});
