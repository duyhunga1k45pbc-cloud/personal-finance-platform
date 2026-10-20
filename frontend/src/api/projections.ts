import { api } from "./client";

export interface ProjectionStatus {
  status: string;
  generation: number | null;
  stored_fingerprint: string | null;
  current_fingerprint: string | null;
  rebuilt_at: string | null;
}

export interface ProjectionSummary {
  generation: number;
  canonical_fingerprint: string;
  total_income: string | number;
  total_expense: string | number;
  balance: string | number;
  net_worth: string | number;
  account_count: number;
  rebuilt_at: string | null;
}

export interface ProjectedAccount {
  account_id: number;
  generation: number;
  balance: string | number;
  rebuilt_at: string | null;
}

export const getProjectionStatus = (signal?: AbortSignal) =>
  api<ProjectionStatus>("/projections/status", { signal });

export const getProjectionSummary = (signal?: AbortSignal) =>
  api<ProjectionSummary>("/projections/summary", { signal });

export const getProjectedAccounts = (signal?: AbortSignal) =>
  api<ProjectedAccount[]>("/projections/accounts", { signal });

export function rebuildProjection(): Promise<ProjectionSummary> {
  return api<ProjectionSummary>("/projections/rebuild", {
    method: "POST",
  });
}
