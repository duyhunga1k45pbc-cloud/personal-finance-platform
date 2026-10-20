import { getAccounts } from "../../api/accounts";
import { formatVND } from "../../lib/money";
import { accountName } from "../../lib/accounts";
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getProjectionStatus,
  getProjectionSummary,
  getProjectedAccounts,
  rebuildProjection,
} from "../../api/projections";
import { sessionStore } from "../../auth/session";

type Props = {
  generation: number;
};

// Deduplicate automatic rebuild attempts within this browser tab.
// Each canonical fingerprint is attempted once per session,
// unless the user explicitly retries after an error.
const attemptedRebuilds = new Set<string>();

export function Dashboard({ generation }: Props) {
  const queryClient = useQueryClient();
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildError, setRebuildError] = useState("");
  const [retryNonce, setRetryNonce] = useState(0);

  const accountsQuery = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
    retry: false,
  });

  const statusQuery = useQuery({
    queryKey: ["projection-status", generation],
    queryFn: ({ signal }) => getProjectionStatus(signal),
    retry: false,
  });

  const status = statusQuery.data;
  const needsRebuild =
    status?.status === "MISSING" ||
    status?.status === "STALE";

  const fingerprint = status?.current_fingerprint;
  const attemptKey =
    `${generation}:${fingerprint ?? ""}:${retryNonce}`;

  useEffect(() => {
    if (!needsRebuild || !fingerprint) return;
    if (attemptedRebuilds.has(attemptKey)) return;

    const current = sessionStore.getSnapshot();
    if (!current.token || current.generation !== generation) {
      return;
    }

    attemptedRebuilds.add(attemptKey);
    let cancelled = false;

    // Invalidate financial values before attempting a rebuild.
    queryClient.removeQueries({
      queryKey: ["projection-summary", generation],
    });
    queryClient.removeQueries({
      queryKey: ["projected-accounts", generation],
    });

    async function run() {
      setRebuilding(true);
      setRebuildError("");

      try {
        await rebuildProjection();

        if (
          cancelled ||
          sessionStore.getSnapshot().generation !== generation
        ) {
          return;
        }

        await queryClient.invalidateQueries({
          queryKey: ["projection-status", generation],
        });
      } catch (error) {
        if (
          cancelled ||
          sessionStore.getSnapshot().generation !== generation
        ) {
          return;
        }

        setRebuildError(
          error instanceof Error ? error.message : String(error)
        );

        // Recheck: a lost response might follow a successful commit.
        await queryClient.invalidateQueries({
          queryKey: ["projection-status", generation],
        });
      } finally {
        if (
          !cancelled &&
          sessionStore.getSnapshot().generation === generation
        ) {
          setRebuilding(false);
        }
      }
    }

    void run();

    return () => {
      cancelled = true;
    };
  }, [
    needsRebuild,
    fingerprint,
    attemptKey,
    generation,
    queryClient,
  ]);

  // Never display balances while freshness is being rechecked
  // or while a rebuild is in progress.
  const isFresh =
    statusQuery.isSuccess &&
    !statusQuery.isFetching &&
    !rebuilding &&
    status?.status === "FRESH";

  const summaryQuery = useQuery({
    queryKey: [
      "projection-summary",
      generation,
      status?.generation,
      fingerprint,
    ],
    queryFn: ({ signal }) => getProjectionSummary(signal),
    enabled: isFresh,
    retry: false,
    staleTime: 0,
  });

  const balancesQuery = useQuery({
    queryKey: [
      "projected-accounts",
      generation,
      status?.generation,
      fingerprint,
    ],
    queryFn: ({ signal }) => getProjectedAccounts(signal),
    enabled: isFresh,
    retry: false,
    staleTime: 0,
  });

  return (
    <section className="dashboard">
      <div className="section-header">
        <h2>Financial Dashboard</h2>
        <button
          onClick={() => void statusQuery.refetch()}
          disabled={statusQuery.isFetching || rebuilding}
        >
          Refresh
        </button>
      </div>

      {statusQuery.isPending && <p>Checking projection...</p>}

      {statusQuery.isError && (
        <p role="alert">
          Projection status unavailable: {statusQuery.error.message}
        </p>
      )}

      {statusQuery.isSuccess && (
        <div className="projection-status">
          <strong>Projection:</strong> {status?.status}
          {" | "}
          Generation: {status?.generation ?? "N/A"}
        </div>
      )}

      {rebuilding && (
        <p role="status">Updating financial balances...</p>
      )}

      {rebuildError && (
        <div role="alert">
          <p>
            Automatic balance update was not confirmed:
            {" "}{rebuildError}
          </p>
          <button
            disabled={rebuilding}
            onClick={() => setRetryNonce((n) => n + 1)}
          >
            Retry balance update
          </button>
        </div>
      )}

      {statusQuery.isSuccess && !isFresh && !rebuilding && (
        <p>
          Financial balances are unavailable until the
          projection is confirmed fresh.
        </p>
      )}

      {isFresh && (
        <>
          {summaryQuery.isPending && <p>Loading summary...</p>}
          {summaryQuery.isError && (
            <p role="alert">
              Summary unavailable: {summaryQuery.error.message}
            </p>
          )}

          {summaryQuery.data && (
            <div className="metric-grid">
              <Metric label="Total income"
                value={formatVND(summaryQuery.data.total_income)} />
              <Metric label="Total expense"
                value={formatVND(summaryQuery.data.total_expense)} />
              <Metric label="Economic balance"
                value={formatVND(summaryQuery.data.balance)} />
              <Metric label="Net worth"
                value={formatVND(summaryQuery.data.net_worth)} />
            </div>
          )}

          <h3>Account Balances</h3>

          {balancesQuery.isPending && <p>Loading balances...</p>}
          {balancesQuery.isError && (
            <p role="alert">
              Balances unavailable: {balancesQuery.error.message}
            </p>
          )}

          {balancesQuery.data?.map((account) => (
            <div className="balance-row" key={account.account_id}>
              <span>{accountName(account.account_id, accountsQuery.data ?? [])}</span>
              <strong>{formatVND(account.balance)}</strong>
            </div>
          ))}
        </>
      )}
    </section>
  );
}

function Metric({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}
