import { useCallback, useRef, useState } from "react";
import {
  emptyHistory,
  pushHistory,
  undo as undoReducer,
  redo as redoReducer,
} from "@/lib/editHistory";
import type { HistoryState } from "@/lib/editHistory";

/**
 * React hook wrapping the pure editHistory reducer.
 *
 * The stacks live in a ref so mutating them does NOT trigger a re-render.
 * `canUndo` / `canRedo` are kept as state so downstream buttons stay reactive.
 *
 * Usage:
 *   const { canUndo, canRedo, push, undo, redo, reset } = useEditHistory<MyContent>();
 */
export function useEditHistory<T>(max?: number): {
  canUndo: boolean;
  canRedo: boolean;
  push: (present: T) => void;
  undo: (present: T) => T | null;
  redo: (present: T) => T | null;
  reset: () => void;
} {
  const historyRef = useRef<HistoryState<T>>(emptyHistory<T>());

  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  /**
   * Sync the two boolean state values from the latest ref contents. Wrapped in useCallback over the
   * stable setState identities so push/undo/redo/reset can list it as a dependency (no
   * exhaustive-deps warning) without being re-created on every render.
   */
  const syncFlags = useCallback((h: HistoryState<T>) => {
    setCanUndo(h.past.length > 0);
    setCanRedo(h.future.length > 0);
  }, []);

  const push = useCallback(
    (present: T) => {
      const next = pushHistory(historyRef.current, present, max ?? 50);
      historyRef.current = next;
      syncFlags(next);
    },
    [max, syncFlags],
  );

  const undo = useCallback(
    (present: T): T | null => {
      const result = undoReducer(historyRef.current, present);
      if (result === null) return null;
      historyRef.current = result.state;
      syncFlags(result.state);
      return result.target;
    },
    [syncFlags],
  );

  const redo = useCallback(
    (present: T): T | null => {
      const result = redoReducer(historyRef.current, present);
      if (result === null) return null;
      historyRef.current = result.state;
      syncFlags(result.state);
      return result.target;
    },
    [syncFlags],
  );

  const reset = useCallback(() => {
    const empty = emptyHistory<T>();
    historyRef.current = empty;
    syncFlags(empty);
  }, [syncFlags]);

  return { canUndo, canRedo, push, undo, redo, reset };
}
