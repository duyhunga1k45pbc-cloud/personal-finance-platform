import { api } from "./client";
import type { Account } from "./accounts";

export type AccountType = "BANK" | "EWALLET" | "CREDIT_CARD" | "CASH";

export interface CreateAccountCommand {
  name: string;
  account_type: AccountType;
  currency: "VND";
}

export function createAccount(
  command: CreateAccountCommand
): Promise<Account> {
  return api<Account>("/accounts", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(command),
  });
}
