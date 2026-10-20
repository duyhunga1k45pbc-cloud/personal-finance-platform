import { api } from "./client";

// Exact transaction fields are not yet established.
// Keep the payload opaque instead of inventing a schema.
export type TransactionRecord = Record<string, unknown>;

export interface TransactionPage {
  total: number;
  skip: number;
  limit: number;
  data: TransactionRecord[];
}

export function getTransactions(
  skip: number,
  limit: number,
  signal?: AbortSignal
) {
  const params = new URLSearchParams({
    skip: String(skip),
    limit: String(limit),
  });

  return api<TransactionPage>(
    `/transactions?${params.toString()}`,
    { signal }
  );
}
