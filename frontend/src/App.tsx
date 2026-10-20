import { ActivityPage } from "./features/activity/ActivityPage";
import { TransferForm } from "./features/transfers/TransferForm";
import { CreateAccountForm } from "./features/accounts/CreateAccountForm";
import { CreateTransactionForm } from "./features/transactions/CreateTransactionForm";
import { ActivityConnection } from "./realtime/ActivityConnection";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";

import {
  useQueryClient,
} from "@tanstack/react-query";

import { login } from "./api/auth";
import { sessionStore } from "./auth/session";
import { AccountsPage } from "./features/accounts/AccountsPage";
import { Dashboard } from "./features/dashboard/Dashboard";
import { TransactionsPage } from "./features/transactions/TransactionsPage";

function LoginForm() {
  const queryClient = useQueryClient();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const mounted = useRef(true);
  const pending = useRef(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;

    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (pending.current) return;

    pending.current = true;
    setLoading(true);
    setError("");

    const request = new AbortController();
    controller.current = request;

    try {
      const result = await login(
        email,
        password,
        request.signal
      );

      if (!mounted.current || request.signal.aborted) {
        return;
      }

      // Verify identity with the backend, not with unverified browser claims.
      const identityResponse = await fetch("/api/auth/me", {
        headers: {
          Authorization: `Bearer ${result.access_token}`,
        },
        signal: request.signal,
      });

      if (!identityResponse.ok) {
        throw new Error("Unable to verify authenticated user");
      }

      const identity: unknown = await identityResponse.json();

      if (
        !identity ||
        typeof identity !== "object" ||
        !("id" in identity) ||
        typeof identity.id !== "number" ||
        !Number.isSafeInteger(identity.id) ||
        identity.id <= 0
      ) {
        throw new Error("Invalid authenticated identity");
      }

      if (!mounted.current || request.signal.aborted) return;

      queryClient.clear();
      sessionStore.login(result.access_token, identity.id);
    } catch (err) {
      if (!mounted.current || request.signal.aborted) {
        return;
      }

      setError(
        err instanceof Error ? err.message : String(err)
      );
    } finally {
      pending.current = false;
      if (controller.current === request) {
        controller.current = null;
      }

      if (mounted.current) {
        setLoading(false);
      }
    }
  }

  return (
    <div className="login-panel">
      <h1>Personal Finance Platform</h1>
      <p>Sign in to your account</p>

      <form onSubmit={handleSubmit}>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />

        <button type="submit" disabled={loading}>
          {loading ? "Signing in..." : "Login"}
        </button>
      </form>

      {error && <p role="alert">{error}</p>}
    </div>
  );
}

type Page = "dashboard" | "accounts" | "transactions" | "transfers" | "activity";

export default function App() {
  const session = useSyncExternalStore(
    sessionStore.subscribe,
    sessionStore.getSnapshot
  );

  const queryClient = useQueryClient();
  const [page, setPage] = useState<Page>("dashboard");

  function handleLogout() {
    // Invalidate identity before pending requests can update UI.
    sessionStore.logout();

    // Cancel/remove all server-state data from previous identity.
    queryClient.clear();
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
        {(["dashboard", "accounts", "transactions", "transfers", "activity"] as Page[])
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
    </main>
  );
}
