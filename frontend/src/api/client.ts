import { sessionStore } from "../auth/session";
import {
  AuthenticationError,
  expireSessionIfCurrent,
  getAccessToken,
} from "../auth/keycloak";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers = new Headers(options.headers);
  let generation: number;
  let token: string;

  try {
    token = await getAccessToken();
    generation = sessionStore.getSnapshot().generation;
    headers.set("Authorization", `Bearer ${token}`);
  } catch (error) {
    if (error instanceof AuthenticationError) {
      throw new ApiError(401, "Authentication is required");
    }
    throw error;
  }

  const response = await fetch(`/api${path}`, {
    ...options,
    headers,
  });

  if (!response.ok) {
    if (response.status === 401) expireSessionIfCurrent(generation, token);
    throw new ApiError(
      response.status,
      `Request failed: ${response.status}`
    );
  }

  return (await response.json()) as T;
}
