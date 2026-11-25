import Keycloak, { type KeycloakInstance } from "keycloak-js";
import { sessionStore } from "./session";

const settings = {
  url: import.meta.env.VITE_KEYCLOAK_URL ?? "http://localhost:18081",
  realm: import.meta.env.VITE_KEYCLOAK_REALM ?? "pfp-e2e",
  clientId: import.meta.env.VITE_KEYCLOAK_CLIENT_ID ?? "moneta",
};

function createKeycloak(): KeycloakInstance {
  return new Keycloak(settings);
}

let keycloak = createKeycloak();
let initialization: Promise<void> | undefined;

export class AuthenticationError extends Error {
  constructor() {
    super("Authentication is required. Please sign in again.");
    this.name = "AuthenticationError";
  }
}

async function resolveOwner(accessToken: string): Promise<number> {
  const response = await fetch("/api/auth/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });

  if (!response.ok) throw new AuthenticationError();

  const identity: unknown = await response.json();
  if (
    !identity ||
    typeof identity !== "object" ||
    !("id" in identity) ||
    typeof identity.id !== "number" ||
    !Number.isSafeInteger(identity.id) ||
    identity.id <= 0
  ) {
    throw new AuthenticationError();
  }

  return identity.id;
}

function clearLocalSession() {
  const current = sessionStore.getSnapshot();
  if (current.token !== null || current.userId !== null) {
    sessionStore.logout();
  }
}

export function expireSessionIfCurrent(generation: number, token?: string) {
  const current = sessionStore.getSnapshot();
  if (
    current.generation !== generation ||
    (token !== undefined && current.token !== token)
  ) return;
  keycloak.clearToken();
  clearLocalSession();
}

export function initializeAuthentication(): Promise<void> {
  if (initialization) return initialization;

  initialization = keycloak
    .init({
      flow: "standard",
      pkceMethod: "S256",
      checkLoginIframe: false,
      enableLogging: false,
    })
    .then(async (authenticated) => {
      if (!authenticated || !keycloak.token) return;

      const ownerId = await resolveOwner(keycloak.token);
      sessionStore.login(keycloak.token, ownerId);
    })
    .catch(() => {
      keycloak.clearToken();
      clearLocalSession();
      keycloak = createKeycloak();
      initialization = undefined;
      throw new AuthenticationError();
    });

  return initialization;
}

export async function beginLogin(): Promise<void> {
  await initializeAuthentication();
  await keycloak.login({ redirectUri: `${window.location.origin}/` });
}

export async function beginLogout(): Promise<void> {
  clearLocalSession();
  try {
    await keycloak.logout({ redirectUri: `${window.location.origin}/` });
  } catch {
    // Local credentials and financial UI are already cleared if the IdP is unavailable.
  } finally {
    keycloak.clearToken();
  }
}

export async function getAccessToken(): Promise<string> {
  await initializeAuthentication();
  const current = sessionStore.getSnapshot();
  if (!current.token || !current.userId || !keycloak.authenticated) {
    throw new AuthenticationError();
  }

  try {
    await keycloak.updateToken(30);
    if (!keycloak.token) throw new AuthenticationError();
    sessionStore.updateToken(keycloak.token);
    return keycloak.token;
  } catch {
    keycloak.clearToken();
    clearLocalSession();
    throw new AuthenticationError();
  }
}
