export type ActivityEvent = {
  id: string | null;
  type: string;
  data: string;
};

export type StreamState =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "unauthorized"
  | "disconnected";

type Options = {
  token: string;
  signal: AbortSignal;
  onEvent: (event: ActivityEvent) => void;
  onState: (state: StreamState) => void;
  onConnected: () => void;
};

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }

    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);

    function abort() {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Aborted", "AbortError"));
    }

    signal.addEventListener("abort", abort, { once: true });
  });
}

export async function runActivityStream({
  token,
  signal,
  onEvent,
  onState,
  onConnected,
}: Options): Promise<void> {
  let cursor: string | null = null;
  let attempt = 0;

  while (!signal.aborted) {
    onState(attempt === 0 ? "connecting" : "reconnecting");

    try {
      const headers = new Headers({
        Authorization: `Bearer ${token}`,
        Accept: "text/event-stream",
      });

      if (cursor !== null) {
        headers.set("Last-Event-ID", cursor);
      }

      const response = await fetch("/api/activity/shared-stream", {
        headers,
        signal,
        cache: "no-store",
      });

      if (response.status === 401 || response.status === 403) {
        onState("unauthorized");
        return;
      }

      if (!response.ok) {
        throw new Error(`SSE HTTP ${response.status}`);
      }

      if (!response.body) {
        throw new Error("SSE response has no body");
      }

      attempt = 0;
      onState("connected");
      onConnected();

      const reader = response.body.getReader();
      const decoder = new TextDecoder();

      let buffer = "";
      let eventType = "";
      let eventId: string | null = null;
      let dataLines: string[] = [];

      function dispatch() {
        if (dataLines.length > 0) {
          // Only accept and acknowledge syntactically valid events.
          const data = dataLines.join("\n");

          try {
            JSON.parse(data);
          } catch {
            eventType = "";
            eventId = null;
            dataLines = [];
            throw new Error("Malformed activity event JSON");
          }

          if (eventId !== null && !/^[0-9]+$/.test(eventId)) {
            throw new Error("Invalid SSE cursor");
          }

          onEvent({
            id: eventId,
            type: eventType || "message",
            data,
          });

          if (eventId !== null) {
            cursor = eventId;
          }
        }

        eventType = "";
        eventId = null;
        dataLines = [];
      }

      function consumeLine(line: string) {
        if (line.endsWith("\r")) {
          line = line.slice(0, -1);
        }

        if (line === "") {
          dispatch();
          return;
        }

        if (line.startsWith(":")) {
          return;
        }

        const colon = line.indexOf(":");

        const field =
          colon === -1 ? line : line.slice(0, colon);

        let value =
          colon === -1 ? "" : line.slice(colon + 1);

        if (value.startsWith(" ")) {
          value = value.slice(1);
        }

        if (field === "data") {
          dataLines.push(value);
        } else if (field === "event") {
          eventType = value;
        } else if (field === "id") {
          eventId = value;
        }
      }

      const HEARTBEAT_TIMEOUT_MS = 45_000;

      while (!signal.aborted) {
        let timer: ReturnType<typeof setTimeout> | undefined;

        try {
          const result = await Promise.race([
            reader.read(),
            new Promise<never>((_, reject) => {
              timer = setTimeout(() => {
                reject(new Error("SSE heartbeat timeout"));
              }, HEARTBEAT_TIMEOUT_MS);
            }),
          ]);

          if (result.done) break;

          buffer += decoder.decode(result.value, { stream: true });
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }

        // Guard against unbounded buffering.
        if (buffer.length > 1_000_000) {
          throw new Error("SSE buffer exceeded limit");
        }

        let newline: number;

        while ((newline = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          consumeLine(line);
        }
      }

      if (signal.aborted) break;

      // A clean EOF can also mean server shutdown.
      throw new Error("SSE stream closed");
    } catch (error) {
      if (signal.aborted) break;

      console.warn("Activity SSE disconnected:", error);
    }

    attempt += 1;

    const backoff = Math.min(
      1000 * 2 ** Math.min(attempt - 1, 4),
      15000
    );

    try {
      await delay(backoff, signal);
    } catch {
      break;
    }
  }

  onState("disconnected");
}
