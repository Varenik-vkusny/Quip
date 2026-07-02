export type EditorAction =
  | "playPause" | "prevClip" | "nextClip" | "render" | "closeOverlay" | "undo" | "redo" | { tab: number };

export function resolveShortcut(e: {
  key: string;
  code?: string;
  target: { tagName?: string; isContentEditable?: boolean };
  ctrl?: boolean;
  meta?: boolean;
  shift?: boolean;
}): EditorAction | null {
  // Typing guard — always first so native text-field undo/redo is never hijacked.
  const t = e.target;
  if (t.isContentEditable || t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT") return null;

  const ctrl  = e.ctrl  ?? false;
  const meta  = e.meta  ?? false;
  const shift = e.shift ?? false;
  const k     = e.key.toLowerCase();
  // `e.key` is keyboard-LAYOUT dependent: on a Cyrillic/non-Latin layout the physical Z key emits
  // "я" (and Y emits "н"), so key-only matching makes Ctrl+Z a silent no-op for our RU users — the
  // Undo BUTTON works (it bypasses this resolver), the keyboard doesn't. `e.code` is the PHYSICAL
  // key ("KeyZ"/"KeyY"), layout-independent → match either so undo/redo fire on any layout.
  const code  = e.code ?? "";

  // Modifier-based shortcuts (undo / redo).
  // When a modifier is held we handle only the known combos; anything else
  // returns null so we don't leak Ctrl+S etc. into the plain-key branch.
  if (ctrl || meta) {
    const isZ = k === "z" || code === "KeyZ";
    const isY = k === "y" || code === "KeyY";
    if (isZ && !shift) return "undo";
    if (isZ &&  shift) return "redo";
    if (isY)           return "redo";
    return null;
  }

  // Plain-key shortcuts (no modifier held).
  switch (e.key) {
    case " ":     return "playPause";
    case "[":     return "prevClip";
    case "]":     return "nextClip";
    case "r":
    case "R":     return "render";
    case "Escape":return "closeOverlay";
    default:
      if (/^[1-6]$/.test(e.key)) return { tab: Number(e.key) };
      return null;
  }
}
