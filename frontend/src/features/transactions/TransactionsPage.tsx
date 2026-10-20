import { formatVND } from "../../lib/money";
import { accountName } from "../../lib/accounts";
import { getAccounts } from "../../api/accounts";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getTransactions } from "../../api/transactions";

type Props = {
  generation: number;
};

const PAGE_SIZE = 10;

function display(value: unknown): string {
  return value === null || value === undefined
    ? "—"
    : String(value);
}

export function TransactionsPage({ generation }: Props) {
  const [skip, setSkip] = useState(0);

  const accountsQuery = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
    retry: false,
  });

  const query = useQuery({
    queryKey: ["transactions", generation, skip, PAGE_SIZE],
    queryFn: ({ signal }) =>
      getTransactions(skip, PAGE_SIZE, signal),
    retry: false,
  });

  return (
    <section className="transactions-section">
      <div className="section-header">
        <h2>Transactions</h2>
        <button
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          {query.isFetching ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {query.isPending && <p>Loading transactions...</p>}

      {query.isError && (
        <p role="alert">
          Failed to load transactions: {query.error.message}
        </p>
      )}

      {query.data && (
        <>
          <p>
            Total: {query.data.total} | Showing from {skip + 1}
          </p>

          <div className="table-scroll">
            <table className="finance-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Type</th>
                  <th>Amount</th>
                  <th>Category</th>
                  <th>Description</th>
                  <th>Account</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody>
                {query.data.data.map((tx) => (
                  <tr key={String(tx.id)}>
                    <td>{display(tx.id)}</td>
                    <td>
                      <span className={`tx-type ${tx.type}`}>
                        {display(tx.type)}
                      </span>
                    </td>
                    <td>{formatVND(display(tx.amount))}</td>
                    <td>{display(tx.category)}</td>
                    <td>{display(tx.description)}</td>
                    <td>{accountName(Number(tx.account_id), accountsQuery.data ?? [])}</td>
                    <td>{display(tx.date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {query.data.data.length === 0 && (
            <p>No transactions found.</p>
          )}

          <div className="pagination">
            <button
              disabled={skip === 0}
              onClick={() =>
                setSkip((value) => Math.max(0, value - PAGE_SIZE))
              }
            >
              Previous
            </button>

            <button
              disabled={skip + PAGE_SIZE >= query.data.total}
              onClick={() =>
                setSkip((value) => value + PAGE_SIZE)
              }
            >
              Next
            </button>
          </div>
        </>
      )}
    </section>
  );
}
