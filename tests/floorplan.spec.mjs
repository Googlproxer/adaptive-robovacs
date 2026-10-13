import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/tests/fixtures/floorplan.html");
  await expect(page.locator("#result")).toHaveAttribute("data-status", "ready");
});

test("stacked floorplans retain scrolling, identity, focus and editing lifecycle", async ({ page }) => {
  await page.getByRole("button", { name: "Run browser regressions" }).click();
  await expect(page.locator("#result")).toHaveAttribute("data-status", "passed");
  await expect(page.locator("#result")).toContainText("PASS:");
});

test("trusted room drag retains capture through movement and pointer release", async ({ page }) => {
  const card = page.locator("adaptive-robovacs-floorplan").first();
  await card.getByRole("button", { name: "Edit plan" }).click();
  const body = card.locator('[data-layer=rooms] > [data-room="lower-a"] > rect').first();
  const box = await body.boundingBox();
  const before = await body.getAttribute("x");
  await page.mouse.move(box.x + box.width * .2, box.y + box.height * .2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * .45, box.y + box.height * .35, { steps: 6 });
  await expect(body).not.toHaveAttribute("x", before);
  await page.mouse.up();
  await expect(card.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
  await expect(card.locator("svg")).toHaveCount(1);
  await card.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(body).toHaveAttribute("x", before);
});

test("touch swipe starting on a room scrolls the page during live updates", async ({ page, context, browserName, isMobile }) => {
  test.skip(browserName !== "chromium" || !isMobile, "Chromium CDP provides native touch-swipe input");
  await page.getByRole("button", { name: "Start live updates" }).click();
  const card = page.locator("adaptive-robovacs-floorplan").first();
  const rect = await card.locator('[data-layer=rooms] > [data-room="lower-a"] > rect').first().boundingBox();
  const client = await context.newCDPSession(page);
  const x = rect.x + rect.width * .5;
  const y = rect.y + rect.height * .8;
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
  for (let i = 1; i <= 8; i++) {
    await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y - i * 18 }] });
    await page.waitForTimeout(25);
  }
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await expect.poll(() => page.evaluate(() => document.scrollingElement.scrollTop)).toBeGreaterThan(40);
  await page.evaluate(() => window.floorplanFixture.stopUpdates());
  await page.waitForTimeout(400);
  const stopped = await page.evaluate(() => document.scrollingElement.scrollTop);
  await page.evaluate(async () => {
    for (let i = 0; i < 20; i++) { window.floorplanFixture.publish("occupancy"); await window.floorplanFixture.frame(); }
  });
  expect(await page.evaluate(() => document.scrollingElement.scrollTop)).toBe(stopped);
});

test("orientation and viewport changes retain geometry without horizontal clipping", async ({ page }) => {
  const card = page.locator("adaptive-robovacs-floorplan").first();
  const body = card.locator('[data-layer=rooms] > [data-room="lower-a"] > rect').first();
  for (const viewport of [{ width: 390, height: 844 }, { width: 844, height: 390 }, { width: 360, height: 740 }]) {
    await page.setViewportSize(viewport);
    await expect(body).toHaveAttribute("x", "3");
    await expect(body).toHaveAttribute("width", "15");
    const dimensions = await page.evaluate(() => ({ width: document.scrollingElement.clientWidth, content: document.scrollingElement.scrollWidth }));
    expect(dimensions.content).toBeLessThanOrEqual(dimensions.width);
    await expect(card.locator("svg")).toHaveCount(1);
  }
});
