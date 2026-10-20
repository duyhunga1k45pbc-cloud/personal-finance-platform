import { api } from "./client";

export type LoginResponse = {
  access_token: string;
  token_type: string;
};

export function login(
  email: string,
  password: string,
  signal?: AbortSignal
): Promise<LoginResponse> {
  return api<LoginResponse>("/auth/login", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ email, password }),
    signal,
  });
}
