import { createHash } from "node:crypto";
import type { Page } from "@playwright/test";

export type MockUser = { id: number; email: string; subject: string };

type Options = {
  users?: MockUser[];
  accessTokenLifetimeSeconds?: number;
  rejectRefresh?: boolean;
};

function encode(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

export async function installKeycloakMock(page: Page, options: Options = {}) {
  const users = options.users ?? [
    { id: 101, email: "test@example.test", subject: "subject-a" },
  ];
  const lifetime = options.accessTokenLifetimeSeconds ?? 300;
  const tokenOwners = new Map<string, MockUser>();
  let selectedUser = users[0];
  let signInIndex = 0;
  let authRequests = 0;
  let refreshRequests = 0;
  let challenge = "";
  let latestToken = "";

  function issueToken(user: MockUser, tokenLifetime: number) {
    const token = `${encode({ alg: "none", typ: "JWT" })}.${encode({
      sub: user.subject,
      exp: Math.floor(Date.now() / 1000) + tokenLifetime,
      iat: Math.floor(Date.now() / 1000),
    })}.test-signature`;
    tokenOwners.set(token, user);
    latestToken = token;
    return token;
  }

  await page.route(
    "**/realms/pfp-e2e/protocol/openid-connect/auth**",
    async (route) => {
      const requestUrl = new URL(route.request().url());
      authRequests += 1;
      challenge = requestUrl.searchParams.get("code_challenge") ?? "";
      if (
        requestUrl.searchParams.get("response_type") !== "code" ||
        requestUrl.searchParams.get("code_challenge_method") !== "S256" ||
        !challenge ||
        requestUrl.searchParams.has("client_secret")
      ) {
        return route.fulfill({ status: 400, body: "Authorization Code with PKCE S256 is required" });
      }

      selectedUser = users[Math.min(signInIndex, users.length - 1)];
      signInIndex += 1;
      const callback = new URL(requestUrl.searchParams.get("redirect_uri")!);
      callback.hash = new URLSearchParams({
        state: requestUrl.searchParams.get("state") ?? "",
        session_state: "playwright-session",
        code: "playwright-auth-code",
        iss: "http://localhost:18081/realms/pfp-e2e",
      }).toString();
      await route.fulfill({ status: 302, headers: { location: callback.toString() } });
    },
  );

  await page.route(
    "**/realms/pfp-e2e/protocol/openid-connect/token",
    async (route) => {
      const form = new URLSearchParams(route.request().postData() ?? "");
      if (form.has("client_secret") || form.get("grant_type") === "password") {
        return route.fulfill({ status: 400, body: "Public clients cannot send a secret or password grant" });
      }
      if (form.get("grant_type") === "authorization_code") {
        const verifier = form.get("code_verifier") ?? "";
        const computed = createHash("sha256")
          .update(verifier)
          .digest("base64url");
        if (!challenge || computed !== challenge) {
          return route.fulfill({ status: 400, body: "PKCE verification failed" });
        }
      } else if (form.get("grant_type") === "refresh_token") {
        refreshRequests += 1;
        if (options.rejectRefresh) {
          return route.fulfill({ status: 400, json: { error: "invalid_grant" } });
        }
      } else {
        return route.fulfill({ status: 400, body: "Unsupported grant type" });
      }

      const token = issueToken(
        selectedUser,
        form.get("grant_type") === "authorization_code" && refreshRequests === 0
          ? lifetime
          : 300,
      );
      await route.fulfill({
        json: {
          access_token: token,
          refresh_token: `${encode({ alg: "none", typ: "JWT" })}.${encode({
            sub: selectedUser.subject,
            exp: Math.floor(Date.now() / 1000) + 1800,
            iat: Math.floor(Date.now() / 1000),
          })}.test-refresh-signature`,
          expires_in: 300,
          refresh_expires_in: 1800,
          token_type: "Bearer",
          session_state: "playwright-session",
        },
      });
    },
  );

  await page.route(
    "**/realms/pfp-e2e/protocol/openid-connect/logout**",
    async (route) => {
      const logout = new URL(route.request().url());
      const redirect = logout.searchParams.get("post_logout_redirect_uri");
      if (redirect) return route.fulfill({ status: 302, headers: { location: redirect } });
      await route.fulfill({ status: 204, body: "" });
    },
  );

  return {
    identityForAuthorization(header?: string): MockUser | undefined {
      const token = header?.replace(/^Bearer\s+/i, "");
      return tokenOwners.get(token ?? "");
    },
    get latestAccessToken() { return latestToken; },
    get authorizationRequestCount() { return authRequests; },
    get refreshRequestCount() { return refreshRequests; },
  };
}
