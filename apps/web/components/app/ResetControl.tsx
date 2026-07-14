"use client";

import { Plus, X } from "lucide-react";
import { useTranslations } from "next-intl";

// The dashboard's "get me out of here" control. It has TWO meanings, and conflating them is what
// made it dangerous: once a job exists it runs server-side (leaving the screen costs nothing), but
// while BYTES ARE STILL IN FLIGHT a reset aborts the XHR *and* drops the IndexedDB resume
// checkpoint — every transferred byte is gone, which on a multi-GB file is minutes of work. So the
// upload case gets its own name ("Cancel upload") and a two-step confirm carrying the % at risk.
// The worst click is at 100% ("preparing"): the whole file is up, the job isn't created yet, and an
// abort throws away the entire transfer.
export function ResetControl({
  uploadPct,
  confirming,
  onArm,
  onDisarm,
  onReset,
}: {
  /** Percent transferred (0-100) while an upload is in flight, or null when there is none. */
  uploadPct: number | null;
  confirming: boolean;
  onArm: () => void;
  onDisarm: () => void;
  onReset: () => void;
}) {
  const t = useTranslations("dashboard");

  // Nothing in flight → a reset is harmless. Plain, unguarded "New project".
  if (uploadPct === null) {
    return (
      <button
        onClick={onReset}
        className="mb-6 inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-muted transition hover:border-line-strong hover:text-ink"
      >
        <Plus className="size-4" />
        {t("newProject")}
      </button>
    );
  }

  return (
    <div className="mb-6">
      {confirming ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-sm text-ink">{t("discardUploadAsk", { pct: uploadPct })}</span>
          <button
            type="button"
            onClick={onReset}
            className="inline-flex items-center rounded-md border border-bad px-3 py-1.5 text-sm font-medium text-bad transition hover:bg-bad hover:text-bg"
          >
            {t("discardUploadYes")}
          </button>
          <button
            type="button"
            onClick={onDisarm}
            className="inline-flex items-center rounded-md border border-line px-3 py-1.5 text-sm text-muted transition hover:border-line-strong hover:text-ink"
          >
            {t("discardUploadNo")}
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={onArm}
          className="inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-muted transition hover:border-line-strong hover:text-ink"
        >
          <X className="size-4" />
          {t("cancelUpload")}
        </button>
      )}
    </div>
  );
}
