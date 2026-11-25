import { expect, test } from "@playwright/test";
import { installKeycloakMock } from "./support/keycloak";

test("unauthenticated user is offered Keycloak sign in without password fields", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("button", { name: "Sign in with Keycloak" }),
  ).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.locator('input[type="email"]')).toHaveCount(0);
});

test("authorization-code login validates the owner through /auth/me and logout clears the session", async ({ page }) => {
  const oidc = await installKeycloakMock(page);
  let identityAuthorization = "";
  let protectedAuthorization = "";
  let localPasswordRequests = 0;

  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const authorization = request.headers().authorization ?? "";
    if (path === "/api/auth/login") localPasswordRequests += 1;
    if (path === "/api/auth/me") {
      identityAuthorization = authorization;
      const user = oidc.identityForAuthorization(authorization);
      return user
        ? route.fulfill({ json: { id: user.id, email: user.email } })
        : route.fulfill({ status: 401, json: { detail: "unauthorized" } });
    }
    protectedAuthorization = authorization;
    if (path.includes("/activity/")) return route.abort();
    if (path === "/api/projections/status") {
      return route.fulfill({ json: { status: "FRESH", generation: 1 } });
    }
    if (path === "/api/projections/summary") {
      return route.fulfill({ json: { total_income: "0", total_expense: "0", balance: "0", net_worth: "0", account_count: 0 } });
    }
    return route.fulfill({ json: [] });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Keycloak" }).click();

  await expect(page.getByRole("button", { name: "Logout" })).toBeVisible();
  await expect.poll(() => identityAuthorization).toMatch(/^Bearer /);
  await expect.poll(() => protectedAuthorization).toBe(`Bearer ${oidc.latestAccessToken}`);
  expect(oidc.authorizationRequestCount).toBe(1);
  expect(localPasswordRequests).toBe(0);

  const persisted = await page.evaluate(() => {
    const all = [localStorage, sessionStorage];
    return all.flatMap((storage) =>
      Array.from({ length: storage.length }, (_, index) => {
        const key = storage.key(index) ?? "";
        return `${key}=${storage.getItem(key) ?? ""}`;
      }),
    ).join("\n");
  });
  expect(persisted).not.toContain(oidc.latestAccessToken);
  expect(persisted).not.toContain("refresh_token");

  await page.getByRole("button", { name: "Logout" }).click();
  await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Logout" })).toHaveCount(0);
});

test("a token rejected by the backend cannot establish an owner session", async ({ page }) => {
  await installKeycloakMock(page);
  let identityCalls = 0;
  await page.route("**/api/auth/me", async (route) => {
    identityCalls += 1;
    await route.fulfill({ status: 401, json: { detail: "invalid token" } });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Keycloak" }).click();

  await expect.poll(() => identityCalls).toBe(1);
  await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Logout" })).toHaveCount(0);
});

test("refreshes an expiring access token before an authenticated API request", async ({ page }) => {
  const oidc = await installKeycloakMock(page, { accessTokenLifetimeSeconds: 5 });
  let protectedAuthorization = "";

  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname;
    const authorization = route.request().headers().authorization ?? "";
    if (path === "/api/auth/me") {
      const user = oidc.identityForAuthorization(authorization);
      return route.fulfill({ json: { id: user?.id ?? 0, email: user?.email ?? "" } });
    }
    if (path.includes("/activity/")) return route.abort();
    protectedAuthorization = authorization;
    return route.fulfill({ json: [] });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Keycloak" }).click();
  await expect(page.getByRole("button", { name: "Logout" })).toBeVisible();
  await expect.poll(() => oidc.refreshRequestCount).toBeGreaterThan(0);
  await expect.poll(() => protectedAuthorization).toBe(`Bearer ${oidc.latestAccessToken}`);
});

test("failed token refresh clears the local authenticated UI", async ({ page }) => {
  const oidc = await installKeycloakMock(page, {
    accessTokenLifetimeSeconds: 5,
    rejectRefresh: true,
  });
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      const user = oidc.identityForAuthorization(route.request().headers().authorization);
      return route.fulfill({ json: { id: user?.id ?? 0, email: user?.email ?? "" } });
    }
    if (path.includes("/activity/")) return route.abort();
    return route.fulfill({ json: [] });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Keycloak" }).click();
  await expect.poll(() => oidc.refreshRequestCount).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Logout" })).toHaveCount(0);
});
