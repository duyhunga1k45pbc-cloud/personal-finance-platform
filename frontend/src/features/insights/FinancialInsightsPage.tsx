import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import {
  getFinancialInsights,
  type FinancialInsightsResponse,
} from "../../api/insights";
import { sessionStore } from "../../auth/session";

type Props = {
  generation: number;
};

type InsightsState =
  | { generation: number; status: "loading" }
  | { generation: number; status: "success"; result: FinancialInsightsResponse }
  | { generation: number; status: "error"; message: string };

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401:
        return "Your session has expired. Please sign in again.";
      case 403:
        return "You do not have permission to view financial insights.";
      case 409:
        return "Your financial projection is out of date. Refresh it from the dashboard, then try again.";
      case 429:
        return "Too many analysis requests. Please wait a moment before trying again.";
      case 502:
        return "The analysis service could not complete this request. Please try again later.";
      case 503:
        return "Financial insights are temporarily unavailable. Please try again later.";
      case 504:
        return "The analysis timed out. Please try again.";
      default:
        return "Unable to load financial insights. Please try again.";
    }
  }

  if (error instanceof TypeError) {
    return "Could not connect to financial insights. Check your connection and try again.";
  }

  return "Unable to load financial insights. Please try again.";
}

export function FinancialInsightsPage({ generation }: Props) {
  const [state, setState] = useState<InsightsState>({
    generation,
    status: "loading",
  });
  const requestId = useRef(0);
  const controller = useRef<AbortController | null>(null);

  const runAnalysis = useCallback(async () => {
    const session = sessionStore.getSnapshot();
    if (!session.token || session.generation !== generation) return;

    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    const activeRequestId = ++requestId.current;
    setState({ generation, status: "loading" });

    try {
      const result = await getFinancialInsights(request.signal);
      const currentSession = sessionStore.getSnapshot();
      if (
        request.signal.aborted ||
        activeRequestId !== requestId.current ||
        currentSession.generation !== generation ||
        !currentSession.token
      ) {
        return;
      }

      setState({ generation, status: "success", result });
    } catch (error) {
      const currentSession = sessionStore.getSnapshot();
      if (
        request.signal.aborted ||
        activeRequestId !== requestId.current ||
        currentSession.generation !== generation ||
        !currentSession.token
      ) {
        return;
      }

      if (error instanceof ApiError && error.status === 401) {
        sessionStore.logout();
        return;
      }

      setState({
        generation,
        status: "error",
        message: errorMessage(error),
      });
    } finally {
      if (activeRequestId === requestId.current) {
        controller.current = null;
      }
    }
  }, [generation]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) void runAnalysis();
    });

    return () => {
      active = false;
      requestId.current += 1;
      controller.current?.abort();
      controller.current = null;
    };
  }, [runAnalysis]);

  const session = sessionStore.getSnapshot();
  const stateIsCurrent =
    state.generation === generation &&
    session.generation === generation &&
    Boolean(session.token);

  async function handleRunAnalysis() {
    await runAnalysis();
  }

  return (
    <section className="insights-page" aria-busy={state.status === "loading"}>
      <div className="section-header">
        <h2>Financial Insights</h2>
        <button onClick={() => void handleRunAnalysis()}>
          {state.status === "loading" ? "Run again" : "Run analysis"}
        </button>
      </div>

      {!stateIsCurrent || state.status === "loading" ? (
        <p role="status">Analyzing your current financial snapshot...</p>
      ) : state.status === "error" ? (
        <p role="alert">{state.message}</p>
      ) : (
        <InsightsResult result={state.result} />
      )}
    </section>
  );
}

function InsightsResult({ result }: { result: FinancialInsightsResponse }) {
  return (
    <div className="insights-result">
      <div className="insights-snapshot" aria-label="Snapshot information">
        <span>Snapshot: {result.snapshot_id}</span>
        <span>Generated: {result.generated_at}</span>
        <span>Freshness: {result.freshness.status}</span>
        <span>Verified: {result.freshness.verified_at}</span>
      </div>

      <section aria-labelledby="insights-summary-heading">
        <h3 id="insights-summary-heading">Summary</h3>
        <p>{result.analysis.summary}</p>
      </section>

      <section aria-labelledby="insights-findings-heading">
        <h3 id="insights-findings-heading">Findings</h3>
        {result.analysis.findings.length === 0 ? (
          <p>No findings were returned for this snapshot.</p>
        ) : (
          <div className="insights-list">
            {result.analysis.findings.map((finding, index) => (
              <article
                className="insight-card"
                key={`${finding.metric}-${index}`}
              >
                <h4>{finding.metric}</h4>
                <p>{finding.description}</p>
                <h5>Evidence</h5>
                <ul>
                  {finding.evidence.map((item, evidenceIndex) => (
                    <li key={`${evidenceIndex}-${item}`}>{item}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="insights-limitations-heading">
        <h3 id="insights-limitations-heading">Limitations</h3>
        {result.analysis.limitations.length === 0 ? (
          <p>No limitations were reported.</p>
        ) : (
          <ul>
            {result.analysis.limitations.map((limitation, index) => (
              <li key={`${index}-${limitation}`}>{limitation}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
