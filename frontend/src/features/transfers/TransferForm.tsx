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

const storageKey = (userId: number) =>
  `pfp:pending-transfer:v1:${userId}`;

function isStoredTransfer(value: unknown): value is {
  key: string;
  payload: TransferCommand;
} {
  if (!value || typeof value !== "object") return false;

  const v = value as Record<string, unknown>;
  const p = v.payload;

  if (typeof v.key !== "string" || !v.key) return false;
  if (!p || typeof p !== "object") return false;

  const command = p as Record<string, unknown>;

  return (
    typeof command.amount === "string" &&
    typeof command.from_account_id === "number" &&
    Number.isSafeInteger(command.from_account_id) &&
    typeof command.to_account_id === "number" &&
    Number.isSafeInteger(command.to_account_id) &&
    (command.description === null ||
      typeof command.description === "string")
  );
}

const AMOUNT_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;

function validAmount(value: string): boolean {
  if (!AMOUNT_RE.test(value)) return false;

  const [whole, fraction = ""] = value.split(".");

  if (whole.length > 16) return false;
  if (whole.length + fraction.length > 18) return false;

  return /[1-9]/.test(whole + fraction);
}

type InitialRecovery = {
  pending: PendingTransfer | null;
  blocked: boolean;
  outcome: Outcome;
};

function readInitialRecovery(
  ownerId: number | null,
  generation: number,
): InitialRecovery {
  if (ownerId === null) {
    return { pending: null, blocked: true, outcome: null };
  }

  try {
    const raw = sessionStorage.getItem(storageKey(ownerId));

    if (!raw) {
      return { pending: null, blocked: false, outcome: null };
    }

    const saved: unknown = JSON.parse(raw);

    if (!isStoredTransfer(saved)) {
      return {
        pending: null,
        blocked: true,
        outcome: {
          kind: "unknown",
          message:
            "Stored transfer could not be validated. Do not submit a duplicate.",
        },
      };
    }

    return {
      pending: {
        key: saved.key,
        payload: saved.payload,
        generation,
      },
      blocked: false,
      outcome: {
        kind: "unknown",
        message:
          "A previous transfer has an unconfirmed outcome. " +
          "Retry the same transfer to resolve it.",
      },
    };
  } catch {
    return {
      pending: null,
      blocked: true,
      outcome: {
        kind: "unknown",
        message:
          "Pending transfer storage could not be read. " +
          "Do not submit a duplicate until the outcome is verified.",
      },
    };
  }
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
  const ownerId = sessionStore.getSnapshot().userId;

  const [initialRecovery] = useState(() =>
    readInitialRecovery(ownerId, generation)
  );

  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(
    initialRecovery.pending !== null
  );
  const [recoveryBlocked] = useState(initialRecovery.blocked);
  const [outcome, setOutcome] = useState<Outcome>(
    initialRecovery.outcome
  );

  const inFlight = useRef(false);
  const pending = useRef<PendingTransfer | null>(
    initialRecovery.pending
  );

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
      setHasPending(false);
      if (ownerId !== null) {
        sessionStorage.removeItem(storageKey(ownerId));
      }

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
        setHasPending(false);
        if (ownerId !== null) {
          sessionStorage.removeItem(storageKey(ownerId));
        }
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

    if (inFlight.current || pending.current || recoveryBlocked) return;

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

    if (ownerId === null) {
      setOutcome({
        kind: "rejected",
        message: "Authenticated owner is unavailable.",
      });
      return;
    }

    try {
      sessionStorage.setItem(
        storageKey(ownerId),
        JSON.stringify({
          key: command.key,
          payload: command.payload,
        }),
      );
    } catch {
      setOutcome({
        kind: "rejected",
        message: "Cannot persist transfer for recovery. No request was sent.",
      });
      return;
    }

    pending.current = command;
    setHasPending(true);
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
            disabled={busy || hasPending || recoveryBlocked}
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
            disabled={busy || hasPending || recoveryBlocked}
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
            disabled={busy || hasPending || recoveryBlocked}
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
            disabled={busy || hasPending || recoveryBlocked}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Transfer to bank"
          />
        </label>

        <button
          type="submit"
          disabled={
            busy ||
            hasPending ||
            recoveryBlocked ||
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
