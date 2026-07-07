import { RotateCcw, Upload } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Eyebrow } from "@/components/ui/Eyebrow";

// Calm status report — the run didn't complete. The message reads as plain body text
// (not a raw mono <pre> dump), so a failure feels like an honest reading from the
// instrument, not a stack trace.
//
// Labels are injected by the caller (the dashboard, which owns the i18n catalog) so this stays a
// pure presentational component. When `onResume` is supplied — e.g. an interrupted large upload that
// can pick up from its checkpoint — Resume becomes the PRIMARY action and `onRetry` (Start over) the
// secondary; otherwise a single retry button is shown.
export function ErrorPanel({
  message,
  onRetry,
  retryLabel = "Try again",
  onResume,
  resumeLabel = "Resume upload",
  eyebrow = "Run stopped",
  title = "Something went wrong",
}: {
  message: string;
  onRetry: () => void;
  retryLabel?: string;
  onResume?: () => void;
  resumeLabel?: string;
  eyebrow?: string;
  title?: string;
}) {
  return (
    <div className="mx-auto w-full max-w-xl rounded-lg border border-bad/40 bg-bad/[0.06] p-6">
      <Eyebrow tone="faint">{eyebrow}</Eyebrow>
      <h2 className="mt-2 font-display text-h3 text-bad">{title}</h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">{message}</p>
      <div className="mt-5 flex flex-wrap gap-2.5">
        {onResume ? (
          <>
            <Button variant="accent" size="sm" onClick={onResume}>
              <Upload className="size-4" />
              {resumeLabel}
            </Button>
            <Button variant="secondary" size="sm" onClick={onRetry}>
              <RotateCcw className="size-4" />
              {retryLabel}
            </Button>
          </>
        ) : (
          <Button variant="accent" size="sm" onClick={onRetry}>
            <RotateCcw className="size-4" />
            {retryLabel}
          </Button>
        )}
      </div>
    </div>
  );
}
