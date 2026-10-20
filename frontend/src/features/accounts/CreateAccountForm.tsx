import { useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  createAccount,
  type AccountType,
} from "../../api/accountCommands";
import { sessionStore } from "../../auth/session";

type Props = {
  generation: number;
};

export function CreateAccountForm({ generation }: Props) {
  const queryClient = useQueryClient();

  const [name, setName] = useState("");
  const [accountType, setAccountType] =
    useState<AccountType>("BANK");

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [uncertain, setUncertain] = useState(false);

  const inFlight = useRef(false);
  const uncertainRef = useRef(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (inFlight.current || uncertainRef.current) return;

    const accountName = name.trim();

    if (!accountName || accountName.length > 100) {
      setMessage("Account name must be between 1 and 100 characters.");
      return;
    }

    inFlight.current = true;
    setBusy(true);
    setMessage("");

    try {
      const account = await createAccount({
        name: accountName,
        account_type: accountType,
        currency: "VND",
      });

      if (sessionStore.getSnapshot().generation !== generation) {
        return;
      }

      setMessage(`Account #${account.id} created successfully.`);
      setName("");

      await queryClient.invalidateQueries({
        queryKey: ["accounts", generation],
      });

      await queryClient.invalidateQueries({
        queryKey: ["projection-status", generation],
      });
    } catch (error) {
      if (sessionStore.getSnapshot().generation !== generation) {
        return;
      }

      uncertainRef.current = true;
      setUncertain(true);

      const detail =
        error instanceof Error ? error.message : String(error);

      setMessage(
        `Account creation was not confirmed (${detail}). ` +
        "Check the Accounts list before submitting again."
      );
    } finally {
      inFlight.current = false;

      if (sessionStore.getSnapshot().generation === generation) {
        setBusy(false);
      }
    }
  }

  async function checkAccounts() {
    await queryClient.invalidateQueries({
      queryKey: ["accounts", generation],
    });
  }

  return (
    <section className="create-transaction-section">
      <h2>Create Account</h2>

      <form onSubmit={handleSubmit} className="transaction-form">
        <label>
          Account name
          <input
            value={name}
            maxLength={100}
            disabled={busy || uncertain}
            onChange={(event) => setName(event.target.value)}
            placeholder="My Bank Account"
            required
          />
        </label>

        <label>
          Account type
          <select
            value={accountType}
            disabled={busy || uncertain}
            onChange={(event) =>
              setAccountType(event.target.value as AccountType)
            }
          >
            <option value="BANK">Bank</option>
            <option value="EWALLET">E-Wallet</option>
            <option value="CREDIT_CARD">Credit Card</option>
            <option value="CASH">Cash</option>
          </select>
        </label>

        <label>
          Currency
          <input value="VND" disabled />
        </label>

        <button type="submit" disabled={busy || uncertain}>
          {busy ? "Creating..." : "Create Account"}
        </button>
      </form>

      {message && <p role="status">{message}</p>}

      {uncertain && (
        <div className="command-result">
          <p>
            The previous request may have committed.
            Check whether the account already exists.
          </p>

          <button onClick={() => void checkAccounts()}>
            Refresh Accounts
          </button>

          <button
            onClick={() => {
              uncertainRef.current = false;
              setUncertain(false);
              setMessage("");
            }}
          >
            I checked — allow a new submission
          </button>
        </div>
      )}
    </section>
  );
}
