import { useQuery } from "@tanstack/react-query";
import { getAccounts } from "../../api/accounts";

type Props = {
  generation: number;
};

export function AccountsPage({ generation }: Props) {
  const {
    data: accounts,
    isPending,
    isError,
    error,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
    retry: false,
  });

  if (isPending) {
    return <p>Loading accounts...</p>;
  }

  if (isError) {
    return (
      <section>
        <p role="alert">Failed to load accounts: {error.message}</p>
        <button onClick={() => void refetch()}>
          Retry
        </button>
      </section>
    );
  }

  return (
    <section>
      <header className="section-header">
        <h2>Accounts</h2>
        <button
          onClick={() => void refetch()}
          disabled={isFetching}
        >
          {isFetching ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      {accounts.length === 0 ? (
        <p>No accounts found.</p>
      ) : (
        <div className="account-grid">
          {accounts.map((account) => (
            <article className="account-card" key={account.id}>
              <h3>{account.name}</h3>
              <p>Type: {account.account_type}</p>
              <p>Currency: {account.currency}</p>
              <p>Version: {account.version}</p>

              {account.is_default && (
                <span className="default-label">
                  Default
                </span>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
