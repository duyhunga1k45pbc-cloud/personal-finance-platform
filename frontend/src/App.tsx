import { ActivityPage } from "./features/activity/ActivityPage";
import { TransferForm } from "./features/transfers/TransferForm";
import { CreateAccountForm } from "./features/accounts/CreateAccountForm";
import { CreateTransactionForm } from "./features/transactions/CreateTransactionForm";
import { ActivityConnection } from "./realtime/ActivityConnection";
import {
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import {
  useQueryClient,
} from "@tanstack/react-query";

import { sessionStore } from "./auth/session";
import {
  beginLogin,
  beginLogout,
  initializeAuthentication,
} from "./auth/keycloak";
import { AccountsPage } from "./features/accounts/AccountsPage";
import { Dashboard } from "./features/dashboard/Dashboard";
import { TransactionsPage } from "./features/transactions/TransactionsPage";
import { FinancialInsightsPage } from "./features/insights/FinancialInsightsPage";

function LoginForm() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void initializeAuthentication().catch(() => {
      if (active) setError("Unable to complete sign in. Please try again.");
    });
    return () => { active = false; };
  }, []);

  async function handleLogin() {
    if (loading) return;
    setLoading(true);
    setError("");
    try {
      await beginLogin();
    } catch {
      setError("Unable to start sign in. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-panel">
      <h1>Personal Finance Platform</h1>
      <p>Sign in to your account</p>

      <button type="button" onClick={() => void handleLogin()} disabled={loading}>
        {loading ? "Redirecting to sign in..." : "Sign in with Keycloak"}
      </button>

      {error && <p role="alert">{error}</p>}
    </div>
  );
}

type Page =
  | "dashboard"
  | "accounts"
  | "transactions"
  | "transfers"
  | "activity"
  | "insights";

export default function App() {
  const session = useSyncExternalStore(
    sessionStore.subscribe,
    sessionStore.getSnapshot
  );

  const queryClient = useQueryClient();
  const [page, setPage] = useState<Page>("dashboard");

  useEffect(() => {
    queryClient.clear();
  }, [queryClient, session.generation]);

  function handleLogout() {
    void beginLogout();
  }

  if (!session.token) {
    return <LoginForm />;
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <h1>Personal Finance Platform</h1>
          <p>React + TypeScript + FastAPI</p>
        </div>

        <button onClick={handleLogout}>Logout</button>
      </header>

      <nav className="app-nav" aria-label="Main navigation">
        {(["dashboard", "accounts", "transactions", "transfers", "activity", "insights"] as Page[])
          .map((item) => (
            <button
              key={item}
              className={page === item ? "active" : ""}
              onClick={() => setPage(item)}
            >
              {item.charAt(0).toUpperCase() + item.slice(1)}
            </button>
          ))}
      </nav>

      <ActivityConnection
        token={session.token}
        generation={session.generation}
      />

      {page === "dashboard" && (
        <Dashboard generation={session.generation} />
      )}

      {page === "accounts" && (
        <>
          <AccountsPage generation={session.generation} />
          <CreateAccountForm generation={session.generation} />
        </>
      )}

      {page === "transactions" && (
        <>
          <CreateTransactionForm generation={session.generation} />
          <TransactionsPage generation={session.generation} />
        </>
      )}

      {page === "activity" && (
        <ActivityPage generation={session.generation} />
      )}

      {page === "transfers" && (
        <TransferForm generation={session.generation} />
      )}

      {page === "insights" && (
        <FinancialInsightsPage generation={session.generation} />
      )}
    </main>
  );
}
