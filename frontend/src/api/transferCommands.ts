import { api } from "./client";

export interface TransferCommand {
  amount: string;
  from_account_id: number;
  to_account_id: number;
  description: string | null;
}

export interface TransferResponse {
  id: number;
  event_type: string;
  amount: string | number;
  from_account_id: number;
  to_account_id: number;
  description: string | null;
  occurred_at: string;
  canonical_version: number;
}

export function createTransfer(
  payload: TransferCommand,
  idempotencyKey: string
): Promise<TransferResponse> {
  return api<TransferResponse>("/transfers", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}
