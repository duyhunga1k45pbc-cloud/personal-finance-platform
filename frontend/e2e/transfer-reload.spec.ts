import { test, expect } from "@playwright/test";

test("ambiguous transfer survives reload with identical key and payload", async ({ page }) => {
  const requests: Array<{ key: string; body: string }> = [];

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;

      if (path === "/api/auth/login") {
        return route.fulfill({
          json: {
            access_token: "test-token",
            token_type: "bearer",
          },
        });
      }

      if (path === "/api/auth/me") {
        return route.fulfill({
          json: { id: 101, email: "test@example.com" },
        });
      }

      if (path === "/api/accounts") {
        return route.fulfill({
          json: [
            {
              id: 101,
              name: "Cash",
              account_type: "CASH",
              currency: "VND",
              is_default: true,
              version: 1,
            },
            {
              id: 102,
              name: "Bank",
              account_type: "BANK",
              currency: "VND",
              is_default: false,
              version: 1,
            },
          ],
        });
      }

      if (path === "/api/transfers") {
        requests.push({
          key: request.headers()["idempotency-key"],
          body: request.postData() ?? "",
        });

        if (requests.length === 1) {
          // Simulate lost HTTP response after command submission.
          return route.abort("failed");
        }

        return route.fulfill({
          json: {
            id: 999,
            event_type: "TRANSFER",
            amount: "20000",
            from_account_id: 101,
            to_account_id: 102,
            description: null,
            occurred_at: "2026-10-20T10:00:00",
            canonical_version: 1,
          },
        });
      }

      if (path.includes("/activity/")) {
        return route.abort();
      }

      if (path === "/api/projections/status") {
        return route.fulfill({
          json: {
            status: "FRESH",
            generation: 1,
            stored_fingerprint: "test",
            current_fingerprint: "test",
          },
        });
      }

      if (path === "/api/projections/summary") {
        return route.fulfill({
          json: {
            total_income: "0",
            total_expense: "0",
            balance: "0",
            net_worth: "0",
            account_count: 2,
            generation: 1,
          },
        });
      }

      if (path === "/api/projections/accounts") {
        return route.fulfill({ json: [] });
      }

      return route.fulfill({ json: {} });
    }
  );

  await page.goto("/");

  await page.locator('input[type="email"]').fill("test@example.com");
  await page.locator('input[type="password"]').fill("password");
  await page.getByRole("button", { name: /login/i }).click();

  await page.getByRole("button", { name: /^transfers$/i }).click();

  await page.getByPlaceholder("20000").fill("20000");

  await page.getByRole("button", { name: /^Transfer Money$/i }).click();

  await expect(
    page.getByRole("button", { name: "Retry same transfer" })
  ).toBeVisible();

  // Reload discards the in-memory session and React component state.
  await page.reload();

  await expect(
    page.locator('input[type="email"]')
  ).toBeVisible();

  // Authenticate again as the same verified owner.
  await page.locator('input[type="email"]').fill("test@example.com");
  await page.locator('input[type="password"]').fill("password");
  await page.getByRole("button", { name: /^login$/i }).click();

  await page.getByRole("button", { name: /^transfers$/i }).click();

  await expect(
    page.getByRole("button", { name: "Retry same transfer" })
  ).toBeVisible();

  await page.getByRole("button", { name: "Retry same transfer" }).click();

  await expect(
    page.getByText("Transfer #999 committed successfully.")
  ).toBeVisible();

  expect(requests).toHaveLength(2);
  expect(requests[0].key).toBeTruthy();
  expect(requests[1].key).toBe(requests[0].key);
  expect(requests[1].body).toBe(requests[0].body);
});
