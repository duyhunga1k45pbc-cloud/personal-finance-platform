import { api } from "./client";

export interface CreateTransactionCommand {
  amount: string;
  description: string;
  category: string;
  type: "income" | "expense";
  account_id: number;
}

export interface CreatedTransaction {
  id: number;
  account_id: number;
  canonical_event_id: number;
  canonical_version: number;
}

export function createTransaction(
  command: CreateTransactionCommand,
  idempotencyKey: string
): Promise<CreatedTransaction> {
  return api<CreatedTransaction>("/transactions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(command),
  });
}
