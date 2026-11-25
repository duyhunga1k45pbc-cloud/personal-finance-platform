import { expect, test, type Page, type Route } from "@playwright/test";
import { installKeycloakMock, type MockUser } from "./support/keycloak";

const successfulInsights = {
  snapshot_id: "snap_v2_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  schema_version: 2,
  generated_at: "2026-11-25T10:00:00+00:00",
  freshness: {
    status: "FRESH",
    generation: 3,
    canonical_fingerprint:
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    checked_at: "2026-11-25T10:00:00+00:00",
    verified_at: "2026-11-25T10:00:01+00:00",
  },
  analysis: {
    summary: "Server-provided summary.",
    findings: [
      {
        metric: "total_expense",
        description: "Server-provided finding.",
        evidence: ["Server-provided evidence."],
      },
    ],
    limitations: ["Server-provided limitation."],
  },
};

async function installApiMocks(
  page: Page,
  insightsHandler: (route: Route) => Promise<void>,
  users?: MockUser[],
) {
  const oidc = await installKeycloakMock(page, users ? { users } : {});
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const path = new URL(route.request().url()).pathname;

      if (path === "/api/auth/me") {
        const user = oidc.identityForAuthorization(route.request().headers().authorization);
        await route.fulfill({
          json: { id: user?.id ?? 0, email: user?.email ?? "" },
        });
      } else if (path === "/api/analytics/insights") {
        await insightsHandler(route);
      } else if (path.includes("/activity/")) {
        await route.abort();
      } else if (path === "/api/projections/status") {
        await route.fulfill({
          json: {
            status: "FRESH",
            generation: 1,
            stored_fingerprint: "test",
            current_fingerprint: "test",
          },
        });
      } else if (path === "/api/projections/summary") {
        await route.fulfill({
          json: {
            total_income: "0",
            total_expense: "0",
            balance: "0",
            net_worth: "0",
            account_count: 0,
            generation: 1,
          },
        });
      } else {
        await route.fulfill({ json: [] });
      }
    },
  );
  return oidc;
}

async function signIn(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Keycloak" }).click();
  await expect(page.getByRole("button", { name: "Logout" })).toBeVisible();
}

test("posts without a body and renders server-provided findings", async ({
  page,
}) => {
  let method = "";
  let body: string | null = "not-checked";
  let authorization = "";

  const oidc = await installApiMocks(page, async (route) => {
    method = route.request().method();
    body = route.request().postData();
    authorization = route.request().headers().authorization ?? "";
    await route.fulfill({ json: successfulInsights });
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();

  await expect(
    page.getByText("Server-provided summary.")
  ).toBeVisible();
  await expect(
    page.getByText("Server-provided finding.")
  ).toBeVisible();
  await expect(
    page.getByText("Server-provided evidence.")
  ).toBeVisible();
  await expect(
    page.getByText("Server-provided limitation.")
  ).toBeVisible();
  expect(method).toBe("POST");
  expect(body).toBeNull();
  expect(authorization).toBe(`Bearer ${oidc.latestAccessToken}`);
});

test("renders loading and empty findings states", async ({ page }) => {
  let release!: () => void;
  const pendingResponse = new Promise<void>((resolve) => {
    release = resolve;
  });
  const emptyResponse = {
    ...successfulInsights,
    analysis: { ...successfulInsights.analysis, findings: [] },
  };

  await installApiMocks(page, async (route) => {
    await pendingResponse;
    await route.fulfill({ json: emptyResponse });
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();

  await expect(
    page.getByText("Analyzing your current financial snapshot...")
  ).toBeVisible();
  release();
  await expect(
    page.getByText("No findings were returned for this snapshot.")
  ).toBeVisible();
});

test("handles authorization, stale context, and service errors", async ({
  page,
}) => {
  const cases = [
    { status: 403, message: "You do not have permission" },
    { status: 409, message: "financial projection is out of date" },
    { status: 429, message: "Too many analysis requests" },
    { status: 502, message: "could not complete this request" },
    { status: 503, message: "temporarily unavailable" },
    { status: 504, message: "timed out" },
  ];

  for (const { status, message } of cases) {
    await test.step(`HTTP ${status}`, async () => {
      await page.unrouteAll();
      await installApiMocks(page, async (route) => {
        await route.fulfill({ status, json: { detail: "private detail" } });
      });
      await signIn(page);
      await page.getByRole("button", { name: "Insights" }).click();
      await expect(page.getByRole("alert")).toContainText(message);
      await expect(page.getByText("private detail")).toHaveCount(0);
      await page.getByRole("button", { name: "Logout" }).click();
    });
  }
});

test("returns to sign-in after an insights request receives 401", async ({
  page,
}) => {
  await installApiMocks(page, async (route) => {
    await route.fulfill({ status: 401, json: { detail: "expired" } });
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();

  await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Logout" })).toHaveCount(0);
});

test("ignores an insights response after logout", async ({ page }) => {
  let requestStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    requestStarted = resolve;
  });

  await installApiMocks(page, async (route) => {
    requestStarted();
    await new Promise((resolve) => setTimeout(resolve, 200));
    if (route.request().failure()) return;
    await route.fulfill({ json: successfulInsights });
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();
  await started;
  await page.getByRole("button", { name: "Logout" }).click();

  await expect(page.getByRole("button", { name: "Sign in with Keycloak" })).toBeVisible();
  await page.waitForTimeout(300);
  await expect(page.getByText("Server-provided summary.")).toHaveCount(0);
});

test("account switching cannot display the previous owner's findings", async ({ page }) => {
  const users = [
    { id: 101, email: "owner-a@example.test", subject: "owner-a" },
    { id: 202, email: "owner-b@example.test", subject: "owner-b" },
  ];
  const oidc = await installApiMocks(page, async (route) => {
    const owner = oidc.identityForAuthorization(route.request().headers().authorization);
    await route.fulfill({
      json: {
        ...successfulInsights,
        analysis: {
          ...successfulInsights.analysis,
          summary: owner?.id === 101 ? "Owner A findings." : "Owner B findings.",
        },
      },
    });
  }, users);

  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();
  await expect(page.getByText("Owner A findings.")).toBeVisible();
  await page.getByRole("button", { name: "Logout" }).click();

  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();
  await expect(page.getByText("Owner B findings.")).toBeVisible();
  await expect(page.getByText("Owner A findings.")).toHaveCount(0);
});

test("a newer analysis supersedes the pending response", async ({ page }) => {
  let releaseFirst!: () => void;
  let firstStarted!: () => void;
  const firstResponse = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const started = new Promise<void>((resolve) => {
    firstStarted = resolve;
  });
  let requests = 0;
  const newerResponse = {
    ...successfulInsights,
    analysis: { ...successfulInsights.analysis, summary: "Newer result." },
  };

  await installApiMocks(page, async (route) => {
    requests += 1;
    if (requests === 1) {
      firstStarted();
      await firstResponse;
      if (route.request().failure()) return;
      await route.fulfill({ json: successfulInsights });
      return;
    }
    await route.fulfill({ json: newerResponse });
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();
  await started;
  await page.getByRole("button", { name: "Run again" }).click();

  await expect(page.getByText("Newer result.")).toBeVisible();
  releaseFirst();
  await page.waitForTimeout(100);
  await expect(page.getByText("Server-provided summary.")).toHaveCount(0);
});

test("shows a clear message when the network request fails", async ({
  page,
}) => {
  await installApiMocks(page, async (route) => {
    await route.abort("failed");
  });
  await signIn(page);
  await page.getByRole("button", { name: "Insights" }).click();

  await expect(page.getByRole("alert")).toContainText(
    "Could not connect to financial insights"
  );
});
