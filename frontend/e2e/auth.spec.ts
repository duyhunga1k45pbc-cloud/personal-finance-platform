import { test, expect } from "@playwright/test";

test("unauthenticated user sees login form", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("button", { name: /login|sign in/i })
  ).toBeVisible();
});

test("invalid credentials do not create an authenticated session", async ({ page }) => {
  let loginRequests = 0;

  await page.route("**/api/auth/login", async (route) => {
    loginRequests++;

    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "Invalid credentials",
      }),
    });
  });

  await page.goto("/");

  await page.locator('input[type="email"]').fill("invalid@example.com");
  await page.locator('input[type="password"]').fill("wrong-password");

  await page.getByRole("button", { name: /login|sign in/i }).click();

  await expect.poll(() => loginRequests).toBe(1);

  await expect(
    page.getByRole("button", { name: /login|sign in/i })
  ).toBeVisible();

  await expect(
    page.getByRole("button", { name: /logout|log out/i })
  ).toHaveCount(0);
});

test("successful login allows logout and clears authenticated UI", async ({ page }) => {
  let authorizationHeader: string | undefined;

  await page.route("**/api/auth/login", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        access_token: "playwright-test-token",
        token_type: "bearer",
      }),
    });
  });

  // Other backend calls are mocked: no real financial data is accessed.
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/auth/login") {
      return route.fallback();
    }

    authorizationHeader ??= request.headers()["authorization"];

    if (url.pathname === "/api/auth/me") {
      return route.fulfill({
        json: { id: 101, email: "test@example.com" },
      });
    }

    if (url.pathname === "/api/accounts") {
      return route.fulfill({ json: [] });
    }

    if (url.pathname === "/api/projections/status") {
      return route.fulfill({
        json: {
          status: "FRESH",
          generation: 1,
          stored_fingerprint: "test",
          current_fingerprint: "test",
        },
      });
    }

    if (url.pathname === "/api/projections/summary") {
      return route.fulfill({
        json: {
          total_income: "0",
          total_expense: "0",
          balance: "0",
          net_worth: "0",
          account_count: 0,
          generation: 1,
        },
      });
    }

    if (url.pathname === "/api/projections/accounts") {
      return route.fulfill({ json: [] });
    }

    if (url.pathname.includes("/activity/")) {
      return route.abort();
    }

    return route.fulfill({ json: {} });
  });

  page.on("pageerror", (error) => {
    console.log("JS ERROR:", error.message);
  });

  page.on("console", (message) => {
    if (message.type() === "error") {
      console.log("CONSOLE ERROR:", message.text());
    }
  });

  page.on("requestfailed", (request) => {
    console.log("REQUEST FAILED:", request.url(), request.failure());
  });

  page.on("response", (response) => {
    if (response.status() >= 400) {
      console.log("HTTP ERROR:", response.status(), response.url());
    }
  });

  await page.goto("/");

  console.log("CURRENT URL:", page.url());
  console.log("PAGE TITLE:", await page.title());
  console.log("VISIBLE BODY:", (await page.locator("body").innerText()).slice(0, 1500));
  console.log("EMAIL INPUT COUNT:", await page.locator('input[type="email"]').count());

  await page.locator('input[type="email"]').fill("test@example.com");
  await page.locator('input[type="password"]').fill("test-password");

  await page.getByRole("button", { name: /login|sign in/i }).click();

  await expect(
    page.getByRole("button", { name: /logout|log out/i })
  ).toBeVisible();

  await expect.poll(() => authorizationHeader).toBe(
    "Bearer playwright-test-token"
  );

  await page.getByRole("button", { name: /logout|log out/i }).click();

  await expect(
    page.getByRole("button", { name: /login|sign in/i })
  ).toBeVisible();

  await expect(
    page.getByRole("button", { name: /logout|log out/i })
  ).toHaveCount(0);
});
