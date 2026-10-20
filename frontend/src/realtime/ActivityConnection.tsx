import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { sessionStore } from "../auth/session";
import {
  runActivityStream,
  type StreamState,
} from "./activitySse";

type Props = {
  token: string;
  generation: number;
};

export function ActivityConnection({
  token,
  generation,
}: Props) {
  const queryClient = useQueryClient();

  const [state, setState] =
    useState<StreamState>("connecting");

  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;

    function isCurrentSession() {
      const current = sessionStore.getSnapshot();

      return (
        !signal.aborted &&
        current.token === token &&
        current.generation === generation
      );
    }

    function refreshServerState() {
      if (!isCurrentSession()) return;

      void queryClient.invalidateQueries({
        queryKey: ["accounts", generation],
      });

      void queryClient.invalidateQueries({
        queryKey: ["transactions", generation],
      });

      void queryClient.invalidateQueries({
        queryKey: ["projection-status", generation],
      });

      // An activity event does not prove the projection is fresh.
      // Discard cached financial values and recheck status.
      queryClient.removeQueries({
        queryKey: ["projection-summary", generation],
      });

      queryClient.removeQueries({
        queryKey: ["projected-accounts", generation],
      });
    }

    void runActivityStream({
      token,
      signal,

      onEvent(event) {
        if (!isCurrentSession()) return;

        console.debug(
          "PFP activity:",
          event.type,
          event.id
        );

        refreshServerState();
      },

      onConnected() {
        if (!isCurrentSession()) return;

        // Reconcile after any connection or reconnection.
        refreshServerState();

        void queryClient.refetchQueries({
          queryKey: ["accounts", generation],
          type: "active",
        });

        void queryClient.refetchQueries({
          queryKey: ["transactions", generation],
          type: "active",
        });

        void queryClient.refetchQueries({
          queryKey: ["projection-status", generation],
          type: "active",
        });
      },

      onState(next) {
        if (!isCurrentSession()) return;
        setState(next);
      },
    });

    return () => {
      controller.abort();
    };
  }, [token, generation, queryClient]);

  return (
    <div className="sse-status" role="status">
      Activity stream: <strong>{state}</strong>
    </div>
  );
}
