import type { Account } from "../api/accounts";

export function accountName(
  accountId: number,
  accounts: Account[]
): string {
  return accounts.find((account) => account.id === accountId)
    ?.name ?? `Account #${accountId}`;
}
