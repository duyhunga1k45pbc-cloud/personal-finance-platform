import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getAccounts } from "../../api/accounts";
import {
  createTransfer,
  type TransferCommand,
} from "../../api/transferCommands";
import { ApiError } from "../../api/client";
import { sessionStore } from "../../auth/session";

type Props = {
  generation: number;
};

type PendingTransfer = {
  key: string;
  payload: TransferCommand;
  generation: number;
};

type Outcome = {
  kind: "success" | "rejected" | "unknown";
  message: string;
} | null;

const AMOUNT_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;

function validAmount(value: string): boolean {
  if (!AMOUNT_RE.test(value)) return false;

  const [whole, fraction = ""] = value.split(".");

  if (whole.length > 16) return false;
  if (whole.length + fraction.length > 18) return false;

  return /[1-9]/.test(whole + fraction);
}

export function TransferForm({ generation }: Props) {
  const queryClient = useQueryClient();

  const { data: accounts = [] } = useQuery({
    queryKey: ["accounts", generation],
    queryFn: ({ signal }) => getAccounts(signal),
    retry: false,
  });

  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  const inFlight = useRef(false);
  const pending = useRef<PendingTransfer | null>(null);

  const sourceId =
    fromId || String(accounts[0]?.id ?? "");

  const destinationId =
    toId || String(
      accounts.find((account) =>
        String(account.id) !== sourceId
      )?.id ?? ""
    );

  async function send(command: PendingTransfer) {
    if (inFlight.current) return;

    inFlight.current = true;
    setBusy(true);
    setOutcome(null);

    try {
      const result = await createTransfer(
        command.payload,
        command.key
      );

      if (sessionStore.getSnapshot().generation !== command.generation) {
        return;
      }

      pending.current = null;

      setOutcome({
        kind: "success",
        message: `Transfer #${result.id} committed successfully.`,
      });

      setAmount("");
      setDescription("");

      // Canonical state changed; old projections cannot be trusted.
      queryClient.removeQueries({
        queryKey: ["projection-summary", generation],
      });

      queryClient.removeQueries({
        queryKey: ["projected-accounts", generation],
      });

      void queryClient.invalidateQueries({
        queryKey: ["projection-status", generation],
      });

      void queryClient.invalidateQueries({
        queryKey: ["transactions", generation],
      });

    } catch (error) {
      if (sessionStore.getSnapshot().generation !== command.generation) {
        return;
      }

      const message =
        error instanceof Error ? error.message : String(error);

      // A documented validation failure is not an uncertain commit.
      if (error instanceof ApiError &&
          [400, 404, 422].includes(error.status)) {
        pending.current = null;
        setOutcome({
          kind: "rejected",
          message: `Transfer rejected: ${message}`,
        });
      } else {
        // Preserve key and immutable payload.
        setOutcome({
          kind: "unknown",
          message:
            `Transfer outcome is not confirmed (${message}). ` +
            "Do not create another transfer with a new key.",
        });
      }
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

    const source = Number(sourceId);
    const destination = Number(destinationId);
    const normalizedAmount = amount.trim();

    if (
      !Number.isSafeInteger(source) ||
      !Number.isSafeInteger(destination) ||
      source <= 0 ||
      destination <= 0 ||
      source === destination
    ) {
      setOutcome({
        kind: "rejected",
        message: "Select two different valid accounts.",
      });
      return;
    }

    if (!validAmount(normalizedAmount)) {
      setOutcome({
        kind: "rejected",
        message: "Amount must be positive with at most 2 decimal places.",
      });
      return;
    }

    const command: PendingTransfer = {
      key: crypto.randomUUID(),
      generation,
      payload: {
        amount: normalizedAmount,
        from_account_id: source,
        to_account_id: destination,
        description: description.trim() || null,
      },
    };

    pending.current = command;
    void send(command);
  }

  function retrySame() {
    const command = pending.current;

    if (
      !command ||
      inFlight.current ||
      command.generation !== sessionStore.getSnapshot().generation
    ) return;

    void send(command);
  }

  return (
    <section className="create-transaction-section">
      <h2>Transfer Money</h2>

      {accounts.length < 2 && (
        <p>Create at least two accounts to transfer money.</p>
      )}

      <form onSubmit={handleSubmit} className="transaction-form">
        <label>
          From account
          <select
            value={sourceId}
            disabled={busy || Boolean(pending.current)}
            onChange={(event) => {
              setFromId(event.target.value);
              setToId("");
            }}
          >
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
        </label>

        <label>
          To account
          <select
            value={destinationId}
            disabled={busy || Boolean(pending.current)}
            onChange={(event) => setToId(event.target.value)}
          >
            {accounts
              .filter((account) => String(account.id) !== sourceId)
              .map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
          </select>
        </label>

        <label>
          Amount (VND)
          <input
            type="text"
            inputMode="decimal"
            value={amount}
            disabled={busy || Boolean(pending.current)}
            onChange={(event) => setAmount(event.target.value)}
            placeholder="20000"
            required
          />
        </label>

        <label>
          Description
          <input
            value={description}
            maxLength={255}
            disabled={busy || Boolean(pending.current)}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Transfer to bank"
          />
        </label>

        <button
          type="submit"
          disabled={
            busy ||
            Boolean(pending.current) ||
            accounts.length < 2
          }
        >
          {busy ? "Transferring..." : "Transfer Money"}
        </button>
      </form>

      {outcome && (
        <div className="command-result" role="status">
          <p>{outcome.message}</p>

          {outcome.kind === "unknown" && (
            <div className="command-actions">
              <button disabled={busy} onClick={retrySame}>
                Retry same transfer
              </button>
              <p>
                The request may already have committed.
                This button reuses the same key and payload.
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
