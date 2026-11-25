import { api } from "./client";

export interface FinancialInsightFinding {
  metric: string;
  description: string;
  evidence: string[];
}

export interface FinancialInsightsAnalysis {
  summary: string;
  findings: FinancialInsightFinding[];
  limitations: string[];
}

export interface FinancialInsightsResponse {
  snapshot_id: string;
  schema_version: number;
  generated_at: string;
  freshness: {
    status: string;
    generation: number;
    canonical_fingerprint: string;
    checked_at: string;
    verified_at: string;
  };
  analysis: FinancialInsightsAnalysis;
}

export function getFinancialInsights(
  signal?: AbortSignal,
): Promise<FinancialInsightsResponse> {
  return api<FinancialInsightsResponse>("/analytics/insights", {
    method: "POST",
    signal,
  });
}
