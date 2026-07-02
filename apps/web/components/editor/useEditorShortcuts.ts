import { useEffect } from "react";
import { type EditorAction, resolveShortcut } from "@/lib/editorShortcuts";

export function useEditorShortcuts(dispatch: (a: EditorAction) => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const action = resolveShortcut({
        key: e.key,
        code: e.code, // physical key — layout-independent (e.key is "я" on a Cyrillic layout)
        target: { tagName: target?.tagName, isContentEditable: target?.isContentEditable },
        ctrl:  e.ctrlKey,
        meta:  e.metaKey,
        shift: e.shiftKey,
      });
      if (action === null) return;
      if (action === "playPause") e.preventDefault(); // stop page scroll on Space
      if (action === "undo" || action === "redo") e.preventDefault(); // stop browser-back / native page undo
      dispatch(action);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dispatch]);
}
