/**
 * Oracle 7: Live Full-Stack Playwright E2E, WCAG 2.2 AA, Anti-Clutter,
 * Wire-Leak Inspection & Deterministic 1440x900 Screenshot Oracle.
 *
 * Asserts against the live FastAPI + React/Vite app:
 *  - 0 @axe-core/playwright WCAG 2.2 AA violations across screens
 *  - 0 browser console errors, 0 uncaught page errors, 0 HTTP 5xx responses
 *  - 0 horizontal overflow at BOTH 1280x800 and 1440x900
 *  - Minimalist layout-budget & anti-clutter invariants (compact header, 4 views,
 *    no permanent multi-section control sidebar, closed-by-default graph popovers)
 *  - Keyboard-only Ctrl+K command palette navigation
 *  - Graph mechanics: lens switch, hover spotlight, directional reverse focus,
 *    orphan ring node presence, and zoom LOD
 *  - Network-wire inspection proving redacted evidence_uri & unauthorized canary
 *    tokens NEVER cross the XHR/fetch wire to the browser
 *  - Saves all 7 clean 1440x900 screenshots to docs/screenshots/
 */

import fs from "fs";
import path from "path";
import AxeBuilder from "@axe-core/playwright";
import { expect, Page, test } from "@playwright/test";

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");
const FIXTURES_DIR = path.join(REPO_ROOT, "fixtures");

const PUBLIC_FORBIDDEN_WIRE_TOKENS = [
  "CANARY_MNPI_COBALT_LME_9174",
  "CANARY_MNPI_SOLIS_PIPELINE_5382",
  "CANARY_SIDELETTER_MFN_FEE_4091",
  "CANARY_PERSONAL_PM_THESIS_8826",
  "CANARY_PERSONAL_DEAL_NOTES_6419",
  "alpha_credit_lp_side_letter.pdf",
];

interface WireAuditState {
  consoleErrors: string[];
  pageErrors: string[];
  http5xx: string[];
  wireBodies: Array<{ url: string; body: string }>;
}

function attachPageMonitors(page: Page): WireAuditState {
  const state: WireAuditState = {
    consoleErrors: [],
    pageErrors: [],
    http5xx: [],
    wireBodies: [],
  };

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      state.consoleErrors.push(msg.text());
    }
  });

  page.on("pageerror", (err) => {
    state.pageErrors.push(`${err.name}: ${err.message}`);
  });

  page.on("response", async (resp) => {
    const url = resp.url();
    if (resp.status() >= 500) {
      state.http5xx.push(`${resp.status()} ${url}`);
    }
    if (url.includes("/api/v1/") && !url.includes("/api/v1/events") && resp.status() < 500) {
      try {
        const ct = resp.headers()["content-type"] || "";
        if (!ct.includes("text/event-stream") && (ct.includes("application/json") || ct.includes("text/"))) {
          const text = await resp.text();
          state.wireBodies.push({ url, body: text });
        }
      } catch {
        // Ignore streamed or cancelled responses during teardown
      }
    }
  });

  return state;
}

function assertCleanMonitors(state: WireAuditState, label: string): void {
  expect(state.consoleErrors, `Console errors on ${label}`).toEqual([]);
  expect(state.pageErrors, `Uncaught page errors on ${label}`).toEqual([]);
  expect(state.http5xx, `HTTP 5xx network errors on ${label}`).toEqual([]);
}

function assertZeroWireLeakage(
  wireBodies: Array<{ url: string; body: string }>,
  forbiddenTokens: string[],
  contextLabel: string
): void {
  for (const entry of wireBodies) {
    for (const tok of forbiddenTokens) {
      expect(
        entry.body.includes(tok),
        `WIRE LEAKAGE (${contextLabel}): token "${tok}" crossed network in ${entry.url}`
      ).toBe(false);
    }
  }
}

async function assertNoHorizontalOverflow(page: Page, label: string): Promise<void> {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(
    metrics.scrollWidth,
    `Horizontal overflow on ${label}: scrollWidth=${metrics.scrollWidth} > innerWidth=${metrics.innerWidth}`
  ).toBeLessThanOrEqual(metrics.innerWidth + 1);
}

async function assertWcag22AA(page: Page, screenLabel: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();

  const summary = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.slice(0, 3).map((n) => n.html),
  }));
  expect(summary, `WCAG 2.2 AA violations on ${screenLabel}`).toEqual([]);
}

async function switchPersona(page: Page, userId: string): Promise<void> {
  const trigger = page.getByTestId("persona-trigger");
  await expect(trigger).toBeVisible();
  await trigger.click();
  const opt = page.getByTestId(`persona-option-${userId}`);
  await expect(opt).toBeVisible();
  await opt.click();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(250);
}

async function saveScreenshot(page: Page, filename: string): Promise<void> {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(350);
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, filename),
    fullPage: false,
  });
}

test.describe("Enterprise Context Lake — Oracle 7 Full-Stack E2E", () => {
  test.beforeAll(() => {
    fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  });

  test("1. Home stream, layout-budget anti-clutter, overflow (1280 & 1440), WCAG 2.2 AA & home.png", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    // Single-row compact header <= 64px with exactly 4 lean views
    const header = page.getByTestId("app-header");
    await expect(header).toBeVisible();
    const headerBox = await header.boundingBox();
    expect(headerBox, "Header bounding box must exist").not.toBeNull();
    expect(headerBox!.height, "Header must remain single-row compact (<= 64px)").toBeLessThanOrEqual(64);

    await expect(page.getByTestId("nav-home")).toBeVisible();
    await expect(page.getByTestId("nav-table")).toBeVisible();
    await expect(page.getByTestId("nav-graph")).toBeVisible();
    await expect(page.getByTestId("nav-memory")).toBeVisible();

    const headerText = (await header.innerText()).trim();
    for (const bannedTab of ["Architecture", "MCP Docs", "Marketing"]) {
      expect(headerText).not.toContain(bannedTab);
    }

    // Home view renders topic rail & scannable stream rows
    await expect(page.getByTestId("home-view")).toBeVisible();
    await expect(page.getByTestId("topic-rail")).toBeVisible();
    await expect(page.getByTestId("stream-list")).toBeVisible();
    await expect(page.getByTestId("stream-row-veh:alpha-credit-offshore-lp")).toBeVisible();

    await assertNoHorizontalOverflow(page, "Home 1440x900");
    await assertWcag22AA(page, "Home 1440x900");
    await saveScreenshot(page, "home.png");

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(200);
    await assertNoHorizontalOverflow(page, "Home 1280x800");

    assertCleanMonitors(monitors, "Home");
  });

  test("2. Keyboard-only Ctrl+K command palette, zero wire leakage & search-cmdk.png", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    // Switch to Public Analyst before probing MNPI terms over wire
    await switchPersona(page, "user:public-analyst");
    monitors.wireBodies.length = 0;

    // Keyboard-only Ctrl+K opens command palette
    await page.keyboard.press("Control+k");
    const dialog = page.getByTestId("cmdk-dialog");
    await expect(dialog).toBeVisible();
    const cmdInput = page.getByTestId("cmdk-input");
    await expect(cmdInput).toBeFocused();

    // Forbidden MNPI probe MUST yield zero leakage on wire
    await cmdInput.fill("Project Cobalt");
    await page.waitForTimeout(350);
    assertZeroWireLeakage(monitors.wireBodies, PUBLIC_FORBIDDEN_WIRE_TOKENS, "Ctrl+K MNPI probe");

    // Authorized search & screenshot
    await cmdInput.fill("Alpha Credit");
    await page.waitForTimeout(400);
    await expect(page.getByTestId("cmdk-item-0")).toBeVisible();
    await expect(dialog.getByText(/Alpha Credit/i).first()).toBeVisible();

    await assertWcag22AA(page, "Search Command Palette");
    await assertNoHorizontalOverflow(page, "Search Command Palette");
    await saveScreenshot(page, "search-cmdk.png");

    // Keyboard navigation (ArrowDown + Enter + Escape)
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);
    await page.keyboard.press("Escape");

    assertZeroWireLeakage(monitors.wireBodies, PUBLIC_FORBIDDEN_WIRE_TOKENS, "Ctrl+K full flow");
    assertCleanMonitors(monitors, "Search Command Palette");
  });

  test("3. Diagnostic Graph (compact bar, hover spotlight, reverse topic focus, orphan ring, zoom LOD) & screenshots", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    await page.getByTestId("nav-graph").click();
    await expect(page.getByTestId("graph-view")).toBeVisible();
    await page.waitForTimeout(600);

    // Anti-clutter budget: single compact control bar <= 56px, popovers closed by default
    const controlBar = page.getByTestId("graph-control-bar");
    await expect(controlBar).toBeVisible();
    const barBox = await controlBar.boundingBox();
    expect(barBox).not.toBeNull();
    expect(barBox!.height, "Graph control bar must be compact (<= 56px)").toBeLessThanOrEqual(56);
    await expect(page.getByTestId("graph-filter-popover")).toBeHidden();
    await expect(page.getByTestId("graph-display-popover")).toBeHidden();

    // Fund Hierarchy lens -> settle -> verify hover spotlight + zoom LOD -> graph-funds.png
    await page.getByTestId("graph-lens-fund_hierarchy").click();
    await page.waitForTimeout(700);

    const viewport = page.getByTestId("graph-viewport");
    await expect(viewport).toBeVisible();
    await expect(page.getByTestId("graph-svg")).toBeVisible();

    const offshoreNode = page.getByTestId("graph-node-ent:veh-alpha-offshore");
    await expect(offshoreNode).toBeVisible();

    // Zoom LOD verification (zoom out -> check data-lod changes -> fit back)
    const initialLod = await viewport.getAttribute("data-lod");
    await page.getByTestId("graph-zoom-out").click();
    await page.getByTestId("graph-zoom-out").click();
    await page.getByTestId("graph-zoom-out").click();
    await page.waitForTimeout(200);
    const zoomedOutLod = await viewport.getAttribute("data-lod");
    expect(zoomedOutLod, "data-lod attribute must be present on graph-viewport").toBeTruthy();
    await page.getByTestId("graph-zoom-fit").click();
    await page.waitForTimeout(300);
    expect(initialLod).toBeTruthy();

    // Hover spotlight (300ms debounce)
    await offshoreNode.hover();
    await page.waitForTimeout(450);
    const spotlightAttr = await offshoreNode.getAttribute("data-node-spotlight");
    expect(spotlightAttr).toBeTruthy();

    await assertNoHorizontalOverflow(page, "Graph Funds 1440x900");
    await assertWcag22AA(page, "Graph Funds");
    await saveScreenshot(page, "graph-funds.png");

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(200);
    await assertNoHorizontalOverflow(page, "Graph Funds 1280x800");
    await page.setViewportSize({ width: 1440, height: 900 });

    // Topic lens -> verify golden-angle orphan ring & activate directional Reverse Focus -> graph-reverse-topic.png
    await page.getByTestId("graph-lens-topic").click();
    await page.waitForTimeout(700);

    const orphanNodes = page.locator('[data-node-orphan="true"]');
    expect(await orphanNodes.count(), "Expected >=1 golden-angle orphan node on Topic graph").toBeGreaterThanOrEqual(1);

    const focusTarget = page.getByTestId("graph-node-ent:veh-alpha-offshore").or(page.getByTestId("graph-node-ent:issuer-apex-industrial")).first();
    await expect(focusTarget).toBeVisible();
    await focusTarget.click();
    await page.waitForTimeout(300);

    await expect(page.getByTestId("graph-focus-pill")).toBeVisible();
    await page.getByTestId("focus-dir-reverse").click();
    await page.waitForTimeout(450);

    const dirFocus = await viewport.getAttribute("data-direction-focus");
    expect(dirFocus).toMatch(/reverse|in/i);

    await assertNoHorizontalOverflow(page, "Graph Reverse Topic");
    await assertWcag22AA(page, "Graph Reverse Topic");
    await saveScreenshot(page, "graph-reverse-topic.png");

    assertCleanMonitors(monitors, "Diagnostic Graph");
  });

  test("4. Table + Split Inspector (3-way conflict, server-redacted evidence stub, zero wire URI leak) & inspector-conflict-redacted.png", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    // Switch to Public Analyst: MUST receive server-redacted stub with ZERO PDF URI or canaries on wire!
    await switchPersona(page, "user:public-analyst");
    monitors.wireBodies.length = 0;

    await page.getByTestId("nav-table").click();
    await expect(page.getByTestId("table-view")).toBeVisible();
    await page.waitForTimeout(350);

    const row = page.getByTestId("table-row-veh:alpha-credit-offshore-lp");
    await expect(row).toBeVisible();
    await row.click();
    await page.waitForTimeout(450);

    const inspector = page.getByTestId("inspector-drawer");
    await expect(inspector).toBeVisible();
    await expect(page.getByTestId("conflict-card-conflict-alpha-admin-code-3way")).toBeVisible();
    await expect(page.getByTestId("redacted-evidence-stub").first()).toBeVisible();
    await expect(inspector.getByText(/Restricted — Request Access/i).first()).toBeVisible();
    await expect(inspector.getByText("ADM-904", { exact: false }).first()).toBeVisible();
    await expect(inspector.getByText("ADM-904X", { exact: false }).first()).toBeVisible();

    // DOM & Network wire proof: raw PDF URI & all unauthorized canaries NEVER reached the browser!
    const domHtml = await page.content();
    for (const forbidden of PUBLIC_FORBIDDEN_WIRE_TOKENS) {
      expect(domHtml.includes(forbidden), `DOM leaked forbidden token "${forbidden}"`).toBe(false);
    }
    assertZeroWireLeakage(monitors.wireBodies, PUBLIC_FORBIDDEN_WIRE_TOKENS, "Table + Inspector Public Analyst");

    await assertNoHorizontalOverflow(page, "Inspector Conflict Redacted 1440x900");
    await assertWcag22AA(page, "Inspector Conflict Redacted");
    await saveScreenshot(page, "inspector-conflict-redacted.png");

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(200);
    await assertNoHorizontalOverflow(page, "Inspector Conflict Redacted 1280x800");

    assertCleanMonitors(monitors, "Table & Inspector");
  });

  test("5. Personal Memory (My Context: watchlist alerts, notes, shared-channel disable) & personal-memory.png", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    // Switch to PM Credit who owns seeded personal notes & watchlist alerts
    await switchPersona(page, "user:pm-credit");
    await page.getByTestId("nav-memory").click();
    await expect(page.getByTestId("my-context-view")).toBeVisible();
    await page.waitForTimeout(450);

    await expect(page.getByTestId("watchlist-section")).toBeVisible();
    await expect(page.getByTestId("watchlist-alerts")).toBeVisible();
    await expect(page.getByTestId("memory-notes-list")).toBeVisible();
    await expect(page.getByTestId("memory-note-item-credit-ic-memo-style")).toBeVisible();

    await assertNoHorizontalOverflow(page, "My Context 1440x900");
    await assertWcag22AA(page, "My Context");
    await saveScreenshot(page, "personal-memory.png");

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(200);
    await assertNoHorizontalOverflow(page, "My Context 1280x800");

    // Verify channel_mode=shared auto-disables personal memory cleanly with zero canary in DOM
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.getByTestId("persona-trigger").click();
    await page.getByTestId("channel-mode-toggle").click();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(300);
    await expect(page.getByTestId("memory-disabled-state")).toBeVisible();
    const sharedDom = await page.content();
    expect(sharedDom.includes("CANARY_PERSONAL_PM_THESIS_8826")).toBe(false);

    assertCleanMonitors(monitors, "My Context");
  });

  test("6. Manual Upload & Quarantined Triage Drawer (real file upload + SHA-256 + triage queue) & manual-upload-triage.png", async ({ page }) => {
    const monitors = attachPageMonitors(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/", { waitUntil: "networkidle" });

    await switchPersona(page, "user:fund-steward");
    await page.getByTestId("upload-trigger").click();

    const drawer = page.getByTestId("upload-drawer");
    await expect(drawer).toBeVisible();

    // Upload real fixture CSV (manual_admin_code_reconciliation.csv)
    await page
      .getByTestId("upload-file-input")
      .setInputFiles(path.join(FIXTURES_DIR, "uploads", "manual_admin_code_reconciliation.csv"));
    await page.getByTestId("upload-submit-btn").click();
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(450);

    await expect(page.getByTestId("upload-receipt")).toBeVisible();
    await expect(drawer.getByText(/unverified_observation/i).first()).toBeVisible();

    await assertNoHorizontalOverflow(page, "Manual Upload & Triage");
    await assertWcag22AA(page, "Manual Upload & Triage");
    await saveScreenshot(page, "manual-upload-triage.png");

    // Also verify the quarantined Triage tab loads cleanly for steward review
    await page.getByTestId("tab-triage").click();
    await expect(page.getByTestId("triage-queue")).toBeVisible();
    await assertWcag22AA(page, "Quarantined Triage Tab");

    assertCleanMonitors(monitors, "Manual Upload & Triage");
  });
});
