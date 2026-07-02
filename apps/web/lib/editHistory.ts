/**
 * Pure, generic undo/redo history reducer.
 *
 * Layout conventions:
 *  - `past`   — array of committed states, oldest at index 0, newest at the end.
 *  - `future`  — array of states reachable by redo, oldest-future at index 0,
 *                most-recently-undone at the end. Mirrors past: redo pops the end.
 *
 * All functions treat inputs as immutable and return new objects.
 */

export type HistoryState<T> = { past: T[]; future: T[] };

/** Create an empty history with no past or future entries. */
export const emptyHistory = <T,>(): HistoryState<T> => ({ past: [], future: [] });

/**
 * Record `present` as a new committed step.
 *
 * Clears `future` (a new edit forks history).
 * Bounds `past` to `max` (default 50) by dropping the oldest entry.
 */
export function pushHistory<T>(
  state: HistoryState<T>,
  present: T,
  max = 50,
): HistoryState<T> {
  const next = [...state.past, present];
  return {
    past: next.length > max ? next.slice(next.length - max) : next,
    future: [],
  };
}

/**
 * Undo: move the most-recent past entry to `target` and push `present` onto `future`.
 *
 * Returns `null` if there is nothing to undo.
 */
export function undo<T>(
  state: HistoryState<T>,
  present: T,
): { target: T; state: HistoryState<T> } | null {
  if (state.past.length === 0) return null;

  const target = state.past[state.past.length - 1];
  return {
    target,
    state: {
      past: state.past.slice(0, -1),
      future: [...state.future, present],
    },
  };
}

/**
 * Redo: symmetric to undo. Moves the last `future` entry to `target` and pushes
 * `present` onto `past`.
 *
 * Returns `null` if there is nothing to redo.
 */
export function redo<T>(
  state: HistoryState<T>,
  present: T,
): { target: T; state: HistoryState<T> } | null {
  if (state.future.length === 0) return null;

  const target = state.future[state.future.length - 1];
  return {
    target,
    state: {
      past: [...state.past, present],
      future: state.future.slice(0, -1),
    },
  };
}
