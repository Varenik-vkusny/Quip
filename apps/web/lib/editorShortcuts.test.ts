import { describe, it, expect } from "vitest";
import { resolveShortcut } from "./editorShortcuts";

// ── target helpers ───────────────────────────────────────────────────────────
const body        = { tagName: "BODY",     isContentEditable: false } as const;
const textarea    = { tagName: "TEXTAREA", isContentEditable: false } as const;
const input       = { tagName: "INPUT",    isContentEditable: false } as const;
const select      = { tagName: "SELECT",   isContentEditable: false } as const;
const contentEdit = { tagName: "DIV",      isContentEditable: true  } as const;

// ── existing plain-key shortcuts — must not regress ──────────────────────────
describe("existing plain-key shortcuts (no modifier)", () => {
  it("Space → playPause",       () => expect(resolveShortcut({ key: " ",       target: body })).toBe("playPause"));
  it("[ → prevClip",            () => expect(resolveShortcut({ key: "[",       target: body })).toBe("prevClip"));
  it("] → nextClip",            () => expect(resolveShortcut({ key: "]",       target: body })).toBe("nextClip"));
  it("r → render",              () => expect(resolveShortcut({ key: "r",       target: body })).toBe("render"));
  it("R → render",              () => expect(resolveShortcut({ key: "R",       target: body })).toBe("render"));
  it("Escape → closeOverlay",   () => expect(resolveShortcut({ key: "Escape",  target: body })).toBe("closeOverlay"));
  it("1 → { tab: 1 }",         () => expect(resolveShortcut({ key: "1",       target: body })).toEqual({ tab: 1 }));
  it("6 → { tab: 6 }",         () => expect(resolveShortcut({ key: "6",       target: body })).toEqual({ tab: 6 }));
  it("7 → null (out of range)", () => expect(resolveShortcut({ key: "7",       target: body })).toBeNull());
  it("unknown key → null",      () => expect(resolveShortcut({ key: "q",       target: body })).toBeNull());
});

// ── undo / redo ──────────────────────────────────────────────────────────────
describe("undo / redo shortcuts", () => {
  it("Ctrl+Z → undo",           () => expect(resolveShortcut({ key: "z", target: body, ctrl: true })).toBe("undo"));
  it("Ctrl+Z (upper) → undo",   () => expect(resolveShortcut({ key: "Z", target: body, ctrl: true })).toBe("undo"));
  it("Cmd+Z → undo (meta)",     () => expect(resolveShortcut({ key: "z", target: body, meta: true })).toBe("undo"));
  it("Ctrl+Shift+Z → redo",     () => expect(resolveShortcut({ key: "z", target: body, ctrl: true, shift: true })).toBe("redo"));
  it("Cmd+Shift+Z → redo",      () => expect(resolveShortcut({ key: "z", target: body, meta: true, shift: true })).toBe("redo"));
  it("Ctrl+Y → redo",           () => expect(resolveShortcut({ key: "y", target: body, ctrl: true })).toBe("redo"));
  it("Cmd+Y → redo",            () => expect(resolveShortcut({ key: "y", target: body, meta: true })).toBe("redo"));
  it("Ctrl+Y (upper) → redo",   () => expect(resolveShortcut({ key: "Y", target: body, ctrl: true })).toBe("redo"));
  it("Ctrl+S → null (unknown modifier combo)", () =>
    expect(resolveShortcut({ key: "s", target: body, ctrl: true })).toBeNull());
});

// ── typing guard — text fields must always return null ───────────────────────
describe("typing guard (text fields return null)", () => {
  it("Ctrl+Z in TEXTAREA → null (native undo preserved)", () =>
    expect(resolveShortcut({ key: "z", target: textarea, ctrl: true })).toBeNull());
  it("Ctrl+Z in INPUT → null", () =>
    expect(resolveShortcut({ key: "z", target: input, ctrl: true })).toBeNull());
  it("Ctrl+Z in SELECT → null", () =>
    expect(resolveShortcut({ key: "z", target: select, ctrl: true })).toBeNull());
  it("Ctrl+Z in contentEditable → null", () =>
    expect(resolveShortcut({ key: "z", target: contentEdit, ctrl: true })).toBeNull());
  it("plain Space in TEXTAREA → null", () =>
    expect(resolveShortcut({ key: " ", target: textarea })).toBeNull());
});
