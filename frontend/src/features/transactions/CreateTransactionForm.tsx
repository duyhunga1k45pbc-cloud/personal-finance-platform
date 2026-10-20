import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getAccounts } from "../../api/accounts";
import {
  createTransaction,
  type CreateTransactionCommand,
} from "../../api/transactionCommands";
import { sessionStore } from "../../auth/session";

type Props = {
  generation: number;
};

type PendingCommand = {
  key: string;
  payload: CreateTransactionCommand;
  generation: number;
};

type Outcome =
  | { kind: "success"; message: string }
  | { kind: "rejected"; message: string }
  | { kind: "unknown"; message: string }
  | null;

const AMOUNT_PATTERN = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;

export function CreateTransactionForm({ generation }: Props) {
  const queryClient = useQueryClient();

  const accountsQuery = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
  });

  const [type, setType] = useState<"income" | "expense">("expense");
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("");
  const [accountId, setAccountId] = useState("");
  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  const pending = useRef<PendingCommand | null>(null);
  const inFlight = useRef(false);

  const accounts = accountsQuery.data ?? [];
  const selectedAccountId =
    accountId || String(accounts.find((a) => a.is_default)?.id ?? accounts[0]?.id ?? "");

  async function submitCommand(command: PendingCommand) {
    if (inFlight.current) return;

    inFlight.current = true;
    setBusy(true);
    setOutcome(null);

    try {
      const result = await createTransaction(
        command.payload,
        command.key
      );

      if (sessionStore.getSnapshot().generation !== command.generation) {
        return;
      }

      pending.current = null;
      setHasPending(false);

      setOutcome({
        kind: "success",
        message: `Transaction #${result.id} created successfully.`,
      });

      setAmount("");
      setDescription("");
      setCategory("");

      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["transactions", generation],
        }),
        queryClient.invalidateQueries({
          queryKey: ["projection-status", generation],
        }),
      ]);

      // Never reuse cached balance after a financial command.
      queryClient.removeQueries({
        queryKey: ["projection-summary", generation],
      });
      queryClient.removeQueries({
        queryKey: ["projected-accounts", generation],
      });
    } catch (error) {
      if (sessionStore.getSnapshot().generation !== command.generation) {
        return;
      }

      const message =
        error instanceof Error ? error.message : String(error);

      // A lost response does not imply that the command failed.
      // Preserve the exact key and payload for an explicit retry.
      setOutcome({
        kind: "unknown",
        message:
          `The command outcome is not confirmed (${message}). ` +
          "Retry with the same command identity, or inspect transactions first.",
      });
    } finally {
      inFlight.current = false;

      if (sessionStore.getSnapshot().generation === command.generation) {
        setBusy(false);
      }
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    if (inFlight.current || pending.current) return;

    const normalizedAmount = amount.trim();

    if (!AMOUNT_PATTERN.test(normalizedAmount) ||
        !/[1-9]/.test(normalizedAmount) ||
        normalizedAmount.replace(".", "").length > 18) {
      setOutcome({
        kind: "rejected",
        message: "Enter a positive amount with at most 2 decimal places.",
      });
      return;
    }

    const parsedAccountId = Number(selectedAccountId);

    if (!Number.isSafeInteger(parsedAccountId) || parsedAccountId <= 0) {
      setOutcome({
        kind: "rejected",
        message: "Select a valid account.",
      });
      return;
    }

    const command: PendingCommand = {
      key: crypto.randomUUID(),
      generation,
      payload: {
        amount: normalizedAmount,
        description,
        category,
        type,
        account_id: parsedAccountId,
      },
    };

    pending.current = command;
    setHasPending(true);
    void submitCommand(command);
  }

  function retrySameCommand() {
    const command = pending.current;
    if (!command || inFlight.current) return;
    if (command.generation !== sessionStore.getSnapshot().generation) return;

    void submitCommand(command);
  }

  function discardUncertainCommand() {
    // This does NOT undo a possibly committed transaction.
    pending.current = null;
    setHasPending(false);
    setOutcome(null);
  }

  return (
    <section className="create-transaction-section">
      <h2>Create Transaction</h2>

      <form onSubmit={handleSubmit} className="transaction-form">
        <label>
          Type
          <select
            value={type}
            disabled={busy || hasPending}
            onChange={(event) =>
              setType(event.target.value as "income" | "expense")
            }
          >
            <option value="expense">Expense</option>
            <option value="income">Income</option>
          </select>
        </label>

        <label>
          Account
          <select
            value={selectedAccountId}
            disabled={busy || hasPending}
            onChange={(event) => setAccountId(event.target.value)}
            required
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name} ({account.currency})
              </option>
            ))}
          </select>
        </label>

        <label>
          Amount
          <input
            type="text"
            inputMode="decimal"
            placeholder="100000"
            value={amount}
            disabled={busy || hasPending}
            onChange={(event) => setAmount(event.target.value)}
            required
          />
        </label>

        <label>
          Category
          <input
            value={category}
            disabled={busy || hasPending}
            onChange={(event) => setCategory(event.target.value)}
            placeholder="Food"
            required
          />
        </label>

        <label>
          Description
          <input
            value={description}
            disabled={busy || hasPending}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Lunch"
            required
          />
        </label>

        <button
          type="submit"
          disabled={busy || hasPending || accounts.length === 0}
        >
          {busy ? "Submitting..." : "Create Transaction"}
        </button>
      </form>

      {outcome && (
        <div className="command-result" role="status">
          <p>{outcome.message}</p>

          {outcome.kind === "unknown" && (
            <div className="command-actions">
              <button
                disabled={busy}
                onClick={retrySameCommand}
              >
                Retry same command
              </button>

              <button
                disabled={busy}
                onClick={discardUncertainCommand}
              >
                Discard local retry
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
