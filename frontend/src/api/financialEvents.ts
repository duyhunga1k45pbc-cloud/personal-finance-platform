import { api } from "./client";

export type FinancialEventType =
  | "INCOME"
  | "EXPENSE"
  | "TRANSFER"
  | "REFUND"
  | "REVERSAL"
  | "ADJUSTMENT";

export type FinancialEntry = {
  account_id: number;
  amount: string;
};

export type FinancialEvent = {
  id: number;
  event_type: FinancialEventType;
  description: string | null;
  category: string | null;
  lifecycle_state: string;
  version: number;
  legacy_transaction_id: number | null;
  occurred_at: string;
  recorded_at: string;
  entries: FinancialEntry[];
};

export type FinancialEventPage = {
  total: number;
  skip: number;
  limit: number;
  data: FinancialEvent[];
};

export function getFinancialEvents(
  skip: number,
  limit: number,
  eventType: string,
  signal?: AbortSignal,
): Promise<FinancialEventPage> {
  const params = new URLSearchParams({
    skip: String(skip),
    limit: String(limit),
  });

  if (eventType) {
    params.set("event_type", eventType);
  }

  return api<FinancialEventPage>(
    `/financial-events?${params}`,
    { signal },
  );
}
