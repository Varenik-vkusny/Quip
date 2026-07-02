"use client";

// Modal gate shown when the user hits Download after making render-required edits
// (framing / crop / aspect / trim / motion). English intentionally — the editor is
// English-first (see CLAUDE.md: editor i18n deferred to a future phase).

export function ConfirmRenderModal({
  open,
  onRerender,
  onDownloadAnyway,
  onClose,
}: {
  open: boolean;
  onRerender: () => void;
  onDownloadAnyway: () => void;
  onClose: () => void;
}) {
  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Re-render before download?"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative mx-4 w-full max-w-sm rounded-xl border border-line bg-surface p-6 shadow-2xl">
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute right-4 top-4 text-muted transition hover:text-ink focus:outline-none focus:ring-2 focus:ring-accent/40"
        >
          ✕
        </button>

        <h2 className="font-display text-base font-semibold text-ink">
          Re-render before download?
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-muted">
          You changed the framing/crop. The current file does not include those changes yet.
        </p>

        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={onRerender}
            className="flex-1 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-bg transition hover:bg-accent-2 focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            Re-render now
          </button>
          <button
            type="button"
            onClick={onDownloadAnyway}
            className="flex-1 rounded-lg border border-line bg-surface-2 px-4 py-2.5 text-sm font-semibold text-ink transition hover:border-line-strong hover:bg-surface-3 focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            Download anyway
          </button>
        </div>
      </div>
    </div>
  );
}
