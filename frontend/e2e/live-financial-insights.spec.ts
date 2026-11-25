import { createHash } from "node:crypto";
import { expect, test, type Request, type Response } from "@playwright/test";

const keycloakUrl = process.env.PFP_E2E_KEYCLOAK_URL;
const username = process.env.PFP_E2E_USERNAME;
const password = process.env.PFP_E2E_PASSWORD;
const expectedOwnerId = Number(process.env.PFP_E2E_OWNER_ID);
const activeSigningKid = process.env.PFP_E2E_ACTIVE_KID;

function isApiPath(value: string, suffix: string): boolean {
  return new URL(value).pathname === `/api${suffix}`;
}

test("live Keycloak to Kong to FastAPI to Go Financial Insights", async ({
  page,
}) => {
  expect(keycloakUrl, "runner must configure Keycloak").toBeTruthy();
  expect(username, "runner must provision a synthetic browser user").toBeTruthy();
  expect(password, "runner must provision a synthetic browser password").toBeTruthy();
  expect(Number.isSafeInteger(expectedOwnerId)).toBe(true);
  expect(expectedOwnerId).toBeGreaterThan(0);
  expect(activeSigningKid).toBeTruthy();

  const browserErrors: string[] = [];
  const apiStatuses: Array<{ method: string; path: string; status: number }> = [];
  let insightsBody: string | null | undefined;
  let insightsResponse: Response | undefined;
  let ownerResponse: Response | undefined;

  page.on("console", (message) => {
    if (message.type() === "error") {
      browserErrors.push(
        message
          .text()
          .replace(/Bearer\s+\S+/gi, "Bearer [redacted]")
          .replace(/[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g, "[JWT redacted]"),
      );
    }
  });
  page.on("pageerror", (error) => browserErrors.push(error.name));
  page.on("request", (request: Request) => {
    const url = request.url();
    if (isApiPath(url, "/analytics/insights")) {
      insightsBody = request.postData();
    }
  });
  page.on("response", (response: Response) => {
    const url = response.url();
    if (new URL(url).pathname.startsWith("/api/")) {
      apiStatuses.push({
        method: response.request().method(),
        path: new URL(url).pathname,
        status: response.status(),
      });
    }
    if (isApiPath(url, "/auth/me")) ownerResponse = response;
    if (isApiPath(url, "/analytics/insights")) insightsResponse = response;
  });

  await test.step("authenticate with Authorization Code and PKCE S256", async () => {
    await page.goto("/");
    const authorizationRequest = page.waitForRequest((request) =>
      request.url().startsWith(
        `${keycloakUrl}/realms/pfp-e2e/protocol/openid-connect/auth`,
      ),
    );
    await page.getByRole("button", { name: "Sign in with Keycloak" }).click();
    const authRequest = await authorizationRequest;
    const authUrl = new URL(authRequest.url());
    expect(authUrl.searchParams.get("response_type")).toBe("code");
    expect(authUrl.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authUrl.searchParams.get("code_challenge")).toBeTruthy();
    expect(authUrl.searchParams.has("client_secret")).toBe(false);

    await page.waitForURL(/protocol\/openid-connect\/auth/);
    await page.locator("#username").fill(username!);
    await page.locator("#password").fill(password!);
    const tokenRequest = page.waitForRequest((request) =>
      request.url().startsWith(
        `${keycloakUrl}/realms/pfp-e2e/protocol/openid-connect/token`,
      ),
    );
    await page.locator("#kc-login").click();
    const tokenForm = new URLSearchParams((await tokenRequest).postData() ?? "");
    expect(tokenForm.get("grant_type")).toBe("authorization_code");
    expect(tokenForm.get("client_id")).toBe("moneta");
    expect(tokenForm.get("code_verifier")).toBeTruthy();
    expect(tokenForm.has("client_secret")).toBe(false);
    expect(tokenForm.get("grant_type")).not.toBe("password");

    await expect(page.getByRole("button", { name: "Logout" })).toBeVisible();
    await expect.poll(() => ownerResponse?.status()).toBe(200);
    const owner = await ownerResponse!.json();
    expect(owner.id).toBe(expectedOwnerId);
  });

  await test.step("create synthetic owner-scoped account and activity", async () => {
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await page.getByLabel("Account name").fill("PFP Live E2E Synthetic Account");
    await page.getByLabel("Account type").selectOption("BANK");
    await page.getByRole("button", { name: "Create Account", exact: true }).click();
    await expect(page.getByText(/Account #\d+ created successfully\./)).toBeVisible();

    await page.getByRole("button", { name: "Transactions", exact: true }).click();
    await page.getByLabel("Type").selectOption("income");
    await page.getByLabel("Amount").fill("1500000");
    await page.getByLabel("Category").fill("Synthetic income");
    await page.getByLabel("Description").fill("Synthetic test income");
    await page.getByRole("button", { name: "Create Transaction", exact: true }).click();
    await expect(page.getByText(/Transaction #\d+ created successfully\./)).toBeVisible();

    await page.getByLabel("Type").selectOption("expense");
    await page.getByLabel("Amount").fill("450000");
    await page.getByLabel("Category").fill("Synthetic expense");
    await page.getByLabel("Description").fill("Synthetic test expense");
    await page.getByRole("button", { name: "Create Transaction", exact: true }).click();
    await expect(page.getByText(/Transaction #\d+ created successfully\./)).toBeVisible();
  });

  await test.step("load fresh owner-scoped dashboard projection", async () => {
    await page.getByRole("button", { name: "Dashboard", exact: true }).click();
    await expect(page.getByText(/Projection:\s*FRESH/)).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".metric-card")).toHaveCount(4);
    await expect(page.getByText("PFP Live E2E Synthetic Account", { exact: true })).toBeVisible();
    const metrics = await page.locator(".metric-card").allInnerTexts();
    expect(metrics[0]).toContain("1.500.000");
    expect(metrics[1]).toContain("450.000");
  });

  await test.step("request and render validated Financial Context v2 findings", async () => {
    await page.getByRole("button", { name: "Insights", exact: true }).click();
    await expect(page.getByText("Analyzing your current financial snapshot...")).toHaveCount(0, {
      timeout: 30_000,
    });
    await expect(page.getByRole("heading", { name: "Summary" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Findings" })).toBeVisible();
    expect(insightsResponse?.status()).toBe(200);
    expect(insightsBody ?? "").toBe("");
    expect(insightsResponse?.headers()["x-request-id"]).toBeTruthy();

    const result = await insightsResponse!.json();
    expect(result.schema_version).toBe(2);
    expect(result.snapshot_id).toMatch(/^snap_v2_[a-f0-9]{64}$/);
    expect(result.freshness.status).toBe("FRESH");
    expect(result.freshness.generation).toBeGreaterThan(0);
    expect(result.freshness.canonical_fingerprint).toMatch(/^[a-f0-9]{64}$/);
    expect(result.freshness.checked_at).toBeTruthy();
    expect(result.freshness.verified_at).toBeTruthy();
    expect(result.analysis.findings).toHaveLength(3);

    for (const finding of result.analysis.findings) {
      expect(finding.evidence.length).toBeGreaterThan(0);
      for (const evidenceId of finding.evidence) {
        expect(evidenceId).toMatch(/^evidence_v2_[a-f0-9]{64}$/);
        await expect(page.locator(".insight-card li", { hasText: evidenceId })).toBeVisible();
      }
    }
    await expect(page.getByLabel("Snapshot information")).toContainText(result.snapshot_id);
    await expect(page.getByLabel("Snapshot information")).toContainText("FRESH");
    await expect(page.locator(".insight-card")).toHaveCount(3);
    await expect(page.locator(".insight-card li")).toHaveCount(3);
    expect(result.analysis.summary).toBeTruthy();

    // Verify token handling stays memory-only after the callback has completed.
    const persistedAuthMaterial = await page.evaluate(() =>
      [localStorage, sessionStorage]
        .flatMap((storage) => Array.from({ length: storage.length }, (_, i) => storage.getItem(storage.key(i)!) ?? ""))
        .join("\n"),
    );
    expect(persistedAuthMaterial).not.toMatch(/access_token|refresh_token|Bearer\s/i);
  });

  await test.step("logout clears protected data and gateway rejects missing/invalid JWTs", async () => {
    await page.getByRole("button", { name: "Logout", exact: true }).click();
    await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
    await expect(page.getByText("Financial Insights", { exact: true })).toHaveCount(0);
    await expect(page.getByText("PFP Live E2E Synthetic Account", { exact: true })).toHaveCount(0);

    const noToken = await page.request.get("/api/auth/me");
    expect(noToken.status()).toBe(401);

    const forged = `${Buffer.from(JSON.stringify({ alg: "RS256", kid: activeSigningKid })).toString("base64url")}.${Buffer.from(JSON.stringify({ iss: `${keycloakUrl}/realms/pfp-e2e`, aud: "pfp-api", exp: 4102444800 })).toString("base64url")}.${createHash("sha256").update("invalid-signature").digest("base64url")}`;
    const invalidToken = await page.request.get("/api/auth/me", {
      headers: { Authorization: `Bearer ${forged}` },
    });
    expect(invalidToken.status()).toBe(401);
  });

  expect(apiStatuses.some((entry) => entry.method === "POST" && entry.path === "/api/analytics/insights" && entry.status === 200)).toBe(true);
  expect(apiStatuses.some((entry) => entry.method === "POST" && entry.path === "/api/accounts" && entry.status === 200)).toBe(true);
  expect(apiStatuses.filter((entry) => entry.method === "POST" && entry.path === "/api/transactions" && entry.status === 200)).toHaveLength(2);
  expect(apiStatuses.some((entry) => entry.method === "POST" && entry.path === "/api/projections/rebuild" && entry.status === 200)).toBe(true);
  expect(apiStatuses.some((entry) => entry.method === "GET" && entry.path === "/api/projections/summary" && entry.status === 200)).toBe(true);
  expect(browserErrors).toEqual([]);
});
