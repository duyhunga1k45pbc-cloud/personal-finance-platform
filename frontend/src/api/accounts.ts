import { api } from "./client";

export type Account = {
  id: number;
  user_id: number;
  name: string;
  account_type: string;
  currency: string;
  is_default: boolean;
  version: number;
};

export function getAccounts(signal?: AbortSignal) {
  return api<Account[]>("/accounts", { signal });
}
