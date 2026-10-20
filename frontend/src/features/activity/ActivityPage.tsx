import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { getAccounts } from "../../api/accounts";
import { getFinancialEvents } from "../../api/financialEvents";
import { accountName } from "../../lib/accounts";
import { formatVND } from "../../lib/money";

type Props = {
  generation: number;
};

const PAGE_SIZE = 20;

const TYPES = [
  "",
  "INCOME",
  "EXPENSE",
  "TRANSFER",
  "REFUND",
  "REVERSAL",
  "ADJUSTMENT",
];

export function ActivityPage({ generation }: Props) {
  const [skip, setSkip] = useState(0);
  const [eventType, setEventType] = useState("");

  const accountsQuery = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
    retry: false,
  });

  const eventsQuery = useQuery({
    queryKey: [
      "financial-events",
      generation,
      eventType,
      skip,
    ],
    queryFn: ({ signal }) =>
      getFinancialEvents(
        skip,
        PAGE_SIZE,
        eventType,
        signal,
      ),
    retry: false,
  });

  const accounts = accountsQuery.data ?? [];
  const page = eventsQuery.data;

  return (
    <section className="activity-page">
      <div className="section-header">
        <h2>Financial Activity</h2>

        <button
          disabled={eventsQuery.isFetching}
          onClick={() => void eventsQuery.refetch()}
        >
          {eventsQuery.isFetching ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      <div className="activity-toolbar">
        <label>
          Event type
          <select
            value={eventType}
            onChange={(event) => {
              setSkip(0);
              setEventType(event.target.value);
            }}
          >
            {TYPES.map((type) => (
              <option key={type} value={type}>
                {type || "All events"}
              </option>
            ))}
          </select>
        </label>

        <span>Total: {page?.total ?? "—"}</span>
      </div>

      {eventsQuery.isPending && <p>Loading activity...</p>}

      {eventsQuery.isError && (
        <p role="alert">
          Failed to load activity: {eventsQuery.error.message}
        </p>
      )}

      {page?.data.length === 0 && (
        <p>No financial events found.</p>
      )}

      <div className="activity-list">
        {page?.data.map((event) => (
          <article className="activity-card" key={event.id}>
            <div className="activity-card-header">
              <div>
                <strong>{event.event_type}</strong>
                <span> · Event #{event.id}</span>
              </div>

              <span>{event.lifecycle_state}</span>
            </div>

            <p>{event.description || "No description"}</p>

            {event.category && (
              <p>Category: {event.category}</p>
            )}

            <div className="activity-entries">
              {event.entries.map((entry, index) => (
                <div
                  className="balance-row"
                  key={`${event.id}-${index}`}
                >
                  <span>
                    {accountName(entry.account_id, accounts)}
                  </span>
                  <strong>{formatVND(entry.amount)}</strong>
                </div>
              ))}
            </div>

            <div className="activity-metadata">
              <span>Version: {event.version}</span>
              <span>Occurred: {event.occurred_at}</span>
            </div>
          </article>
        ))}
      </div>

      {page && (
        <div className="pagination">
          <button
            disabled={skip === 0}
            onClick={() => setSkip(Math.max(0, skip - PAGE_SIZE))}
          >
            Previous
          </button>

          <button
            disabled={skip + PAGE_SIZE >= page.total}
            onClick={() => setSkip(skip + PAGE_SIZE)}
          >
            Next
          </button>
        </div>
      )}
    </section>
  );
}
