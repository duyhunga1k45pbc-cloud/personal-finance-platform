export type Session = {
  token: string | null;
  userId: number | null;
  generation: number;
};

let state: Session = {
  token: null,
  userId: null,
  generation: 0,
};

const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) {
    listener();
  }
}

export const sessionStore = {
  getSnapshot(): Session {
    return state;
  },

  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },

  login(token: string, userId: number) {
    state = {
      token,
      userId,
      generation: state.generation + 1,
    };
    emit();
  },

  logout() {
    state = {
      token: null,
      userId: null,
      generation: state.generation + 1,
    };
    emit();
  },
};
