"use client";

import { Link2, Minus, Plus, Scissors, Upload, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { IconButton } from "@/components/ui/IconButton";
import { Numeral } from "@/components/ui/Numeral";
import { cn } from "@/lib/cn";
import { isAcceptedVideoFile } from "@/lib/videoFile";

const MIN_CLIPS = 1;
const MAX_CLIPS = 30; // продуктовый потолок (Auto = «сколько найдётся, до 30»)
const DEFAULT_CLIPS = 8;
const DEFAULT_MIN_SEC = 20;
const DEFAULT_MAX_SEC = 60;
const LS_KEY_MIN = "quip:clipMinSec";
const LS_KEY_MAX = "quip:clipMaxSec";
const storedMin = typeof window !== "undefined" ? Number(window.localStorage.getItem(LS_KEY_MIN)) : null;
const storedMax = typeof window !== "undefined" ? Number(window.localStorage.getItem(LS_KEY_MAX)) : null;
const INIT_MIN_SEC = storedMin && storedMin >= 4 && storedMin <= 180 ? storedMin : DEFAULT_MIN_SEC;
const INIT_MAX_SEC = storedMax && storedMax >= 10 && storedMax <= 180 ? storedMax : DEFAULT_MAX_SEC;
const MIN_CLIP_SEC = 4;
const MAX_CLIP_SEC = 180;
// The worker pins clip_max_sec >= 10 (main.py request models). Keep the stepper from
// reaching a value the server would 422 on, so "short clips" never breaks the upload.
const MIN_CLIP_MAX_SEC = 10;
// Quip is for long-form (podcasts, talks). Uploads >100 MB go via R2 multipart (parallel,
// resumable parts) so the old 5 GB single-PUT ceiling is gone — the REAL limit is length
// (3 h, worker-side billing.MAX_VIDEO_MINUTES). This MB number is just a sane guard against
// absurd uploads (a 3 h 1080p source is ~2–6 GB); bump it freely.
const MAX_UPLOAD_MB = 10000;
const MAX_UPLOAD_LABEL = "10 GB";

// YouTube-link import is DISABLED for now. Server-side download bot-gates on Modal's DC-IP often
// enough that users would just get an error in the face, so we hide the entry point until it's
// solved properly (residential proxy / founder-IP tunnel). The WHOLE path behind this — the form
// handler, createJob(youtube), the worker download + multi-cookie fallback — is intact; flip this
// to true to restore the "or paste a link" field. See docs/JOURNAL.md 2026-06-25.
const YOUTUBE_LINK_ENABLED: boolean = false;

/** Client-side YouTube-URL sanity check (server still validates + downloads best-effort).
 *  Loose by design: matches youtube.com / youtu.be and asks for a plausible length. */
function isLikelyYoutubeUrl(url: string): boolean {
  return /youtu\.?be/i.test(url) && url.trim().length > 12;
}

export function SourceForm({
  onSubmit,
  onSubmitFile,
  busy,
}: {
  // onSubmit = YouTube-link path (best-effort import). Optional for back-compat: callers that
  // don't pass it simply don't offer the link field. Upload is the primary affordance.
  onSubmit?: (url: string, maxClips: number, clipMinSec?: number, clipMaxSec?: number) => void;
  onSubmitFile: (file: File, maxClips: number, language: string | null, clipMinSec?: number, clipMaxSec?: number) => void;
  busy: boolean;
}) {
  const t = useTranslations("sourceForm");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  // YouTube link — a SECONDARY option under the drop-zone (best-effort; may fall back to upload).
  const [url, setUrl] = useState("");
  // auto = «сколько найдётся, максимум 30» (без жёсткого выбора числа); custom = ровно N клипов.
  const [auto, setAuto] = useState(true);
  const [count, setCount] = useState(DEFAULT_CLIPS);
  const [dragging, setDragging] = useState(false);
  const [kazakh, setKazakh] = useState(false);
  const [clipMinSec, setClipMinSec] = useState(INIT_MIN_SEC);
  const [clipMaxSec, setClipMaxSec] = useState(INIT_MAX_SEC);
  // The length fields keep a free-text draft so you can clear the box / delete the
  // last digit while typing; the numeric value is committed (clamped) on blur, on a
  // +/- step, and at submit. Without this a controlled number input snaps back and
  // refuses an empty/partial state.
  const [minDraft, setMinDraft] = useState(String(INIT_MIN_SEC));
  const [maxDraft, setMaxDraft] = useState(String(INIT_MAX_SEC));
  // Duration (seconds) of the picked file, probed client-side, so we can warn BEFORE
  // upload if it's shorter than the minimum clip length. null = unknown / not probable.
  const [fileDuration, setFileDuration] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const urlValid = isLikelyYoutubeUrl(url);
  // Pre-flight: a video shorter than the minimum clip length is an impossible request
  // (the pipeline would fail deep in select). Catch it here so we never upload+process
  // it. Fail-open when the duration is unknown (null) — the worker still guards it.
  const videoTooShort = file != null && fileDuration != null && fileDuration < clipMinSec;
  // A file always wins; otherwise a valid YouTube link (only if the link path is wired in).
  const canSubmit = !busy && !videoTooShort && (file != null || (onSubmit != null && urlValid));
  const clamp = (n: number) => Math.max(MIN_CLIPS, Math.min(MAX_CLIPS, n));
  // Auto → шлём потолок (30): воркер вернёт ДО стольких сильных моментов, сколько найдёт.
  const clipsRequested = auto ? MAX_CLIPS : count;
  // Clip-length clamps for the typed inputs. min stays in [MIN_CLIP_SEC, max-2];
  // max stays in [max(MIN_CLIP_MAX_SEC, min+2), MAX_CLIP_SEC] (MIN_CLIP_MAX_SEC
  // mirrors the worker's ge=10, so a typed value can never 422). Applied on blur
  // and again at submit (Enter-to-submit can skip the input blur).
  const clampMinSec = (n: number) => Math.min(clipMaxSec - 2, Math.max(MIN_CLIP_SEC, n || MIN_CLIP_SEC));
  const clampMaxSec = (min: number, n: number) =>
    Math.min(MAX_CLIP_SEC, Math.max(Math.max(MIN_CLIP_MAX_SEC, min + 2), n || MIN_CLIP_MAX_SEC));
  // Commit a number to min/max: clamp it, then sync BOTH the numeric state and its
  // draft string (so a +/- step or a blur snaps the visible field to the clamped value).
  const applyMin = (n: number) => {
    const v = clampMinSec(n);
    setClipMinSec(v);
    setMinDraft(String(v));
    window.localStorage.setItem(LS_KEY_MIN, String(v));
  };
  const applyMax = (n: number) => {
    const v = clampMaxSec(clipMinSec, n);
    setClipMaxSec(v);
    setMaxDraft(String(v));
    window.localStorage.setItem(LS_KEY_MAX, String(v));
  };

  function pickFile(f: File | null) {
    setFileError(null);
    setFileDuration(null);
    if (!f) {
      setFile(null);
      return;
    }
    // Accept by MIME OR (empty MIME + video extension): browsers report "" for some valid
    // containers (.mkv/.mov/.webm/.avi), and the old MIME-only check silently rejected them.
    if (!isAcceptedVideoFile(f.name, f.type)) {
      setFileError(t("notVideo"));
      return;
    }
    if (f.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setFileError(t("tooLarge", { label: MAX_UPLOAD_LABEL }));
      return;
    }
    setFile(f);
    // Probe the duration off a throwaway <video> so the min-length pre-flight can fire
    // before we ever upload. Best-effort: containers the browser can't decode leave it null.
    const probe = document.createElement("video");
    probe.preload = "metadata";
    const objUrl = URL.createObjectURL(f);
    probe.onloadedmetadata = () => {
      const d = probe.duration;
      setFileDuration(Number.isFinite(d) && d > 0 ? d : null);
      URL.revokeObjectURL(objUrl);
    };
    probe.onerror = () => {
      setFileDuration(null);
      URL.revokeObjectURL(objUrl);
    };
    probe.src = objUrl;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    // A picked file is the primary path; a YouTube link is the best-effort secondary path.
    // Parse the drafts too: Enter-to-submit can fire before the input's blur commits.
    const pMin = parseInt(minDraft, 10);
    const pMax = parseInt(maxDraft, 10);
    const safeMin = clampMinSec(Number.isNaN(pMin) ? clipMinSec : pMin);
    const safeMax = clampMaxSec(safeMin, Number.isNaN(pMax) ? clipMaxSec : pMax);
    window.localStorage.setItem(LS_KEY_MIN, String(safeMin));
    window.localStorage.setItem(LS_KEY_MAX, String(safeMax));
    if (file) {
      onSubmitFile(file, clipsRequested, kazakh ? "kk" : null, safeMin, safeMax);
    } else if (onSubmit && urlValid) {
      onSubmit(url.trim(), clipsRequested, safeMin, safeMax);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="w-full">
      {/* Intake: a measured instrument frame (corner ticks + a 9:16 target marker), not a
          limp centered dashed box. Or the selected-file plate once a file is chosen. */}
      {file == null ? (
        <div
          onClick={() => !busy && inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            if (!busy) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (!busy) pickFile(e.dataTransfer.files?.[0] ?? null);
          }}
          role="button"
          tabIndex={0}
          aria-label={t("uploadAria")}
          className={cn(
            "group relative cursor-pointer overflow-hidden rounded-lg border bg-surface px-5 py-7 transition duration-200 ease-snappy",
            dragging ? "border-accent bg-surface-2" : "border-line hover:border-line-strong",
            busy && "pointer-events-none opacity-50",
          )}
        >
          {/* corner ticks — the measured-frame signature */}
          {(["left-2.5 top-2.5 border-l border-t", "right-2.5 top-2.5 border-r border-t", "left-2.5 bottom-2.5 border-l border-b", "right-2.5 bottom-2.5 border-r border-b"] as const).map(
            (pos) => (
              <span
                key={pos}
                aria-hidden
                className={cn(
                  "pointer-events-none absolute size-3 transition-colors",
                  pos,
                  dragging ? "border-accent" : "border-line-strong",
                )}
              />
            ),
          )}
          <div className="flex items-center gap-4">
            {/* 9:16 target marker */}
            <span
              aria-hidden
              className={cn(
                "grid h-14 w-[31.5px] shrink-0 place-items-center rounded-sm border transition-colors",
                dragging ? "border-accent bg-accent-tint" : "border-line-strong bg-surface-2",
              )}
            >
              {/* the 9:16 target marker is structure, not a CTA — keep coral scarce for the
                  "choose a file" affordance + the Make clips button. */}
              <Upload className={cn("size-4 transition-colors", dragging ? "text-accent" : "text-muted")} />
            </span>
            <div className="min-w-0">
              <Eyebrow tone="faint">{t("intake")}</Eyebrow>
              <p className="mt-1.5 text-sm text-ink">
                {dragging
                  ? t("dropToLoad")
                  : t.rich("dropOrChoose", {
                      choose: (chunks) => <span className="font-medium text-accent">{chunks}</span>,
                    })}
              </p>
              <p className="mt-1 font-mono text-eyebrow uppercase tabular-nums text-faint">
                {t("formats", { label: MAX_UPLOAD_LABEL })}
              </p>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex items-center justify-between gap-2 rounded-lg border border-line bg-surface px-4 py-3.5">
          <span className="flex min-w-0 items-center gap-2.5 truncate text-sm text-ink">
            <Upload className="size-4 shrink-0 text-accent" />
            <span className="truncate font-medium">{file.name}</span>
          </span>
          <IconButton
            onClick={() => pickFile(null)}
            disabled={busy}
            aria-label={t("removeFile")}
            size="sm"
            className="size-10 sm:size-7"
          >
            <X className="size-4" />
          </IconButton>
        </div>
      )}
      <input
        ref={inputRef}
        type="file"
        // video/* PLUS explicit extensions: some OS dialogs grey out valid containers (.mkv/.mov/
        // .webm/.avi) under a bare `video/*` because they report no MIME for them.
        accept="video/*,.mp4,.m4v,.mov,.qt,.webm,.mkv,.avi,.wmv,.flv,.mpg,.mpeg,.ts,.mts,.m2ts,.3gp,.3g2,.ogv"
        hidden
        onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
      />
      {fileError ? <p className="mt-2 text-sm text-bad">{fileError}</p> : null}

      {/* Quiet reliability reassurance under the drop-zone: a direct file upload is the surest path
          to clips (the YouTube-link import is intentionally hidden). Only while the drop-zone is
          shown and nothing's wrong — don't stack it over a file plate or an error. */}
      {file == null && !fileError ? (
        <p className="mt-2 text-xs leading-relaxed text-faint">{t("reliabilityNote")}</p>
      ) : null}

      {/* Secondary option: a YouTube link (best-effort import — may not work for every video, in
          which case we ask you to upload the file). Hidden once a file is picked (file wins).
          Only rendered when the link path is wired (onSubmit) so upload-only callers stay clean. */}
      {YOUTUBE_LINK_ENABLED && onSubmit && file == null ? (
        <div className="mt-4">
          <div className="flex items-center gap-3">
            <span className="h-px flex-1 bg-line" />
            <span className="font-mono text-eyebrow uppercase tracking-wide text-faint">
              {t("orPasteLink")}
            </span>
            <span className="h-px flex-1 bg-line" />
          </div>
          {/* The INPUT is the single bordered element (icon sits inside, absolute) so the global
              :focus-visible ring hugs that one border = one box. The old wrapper-label-with-border +
              ring-on-input gave a DOUBLE border (the global ring is unlayered → can't be killed by a
              layered utility). Mirrors components/ui/Input: border stays neutral, coral = the ring. */}
          <div className="relative mt-3">
            <Link2
              className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted"
              aria-hidden
            />
            <input
              type="text"
              inputMode="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={busy}
              aria-label={t("youtubeAria")}
              aria-invalid={url.length > 0 && !urlValid}
              placeholder={t("youtubePlaceholder")}
              className={cn(
                "h-11 w-full rounded-lg border bg-surface pl-10 pr-3.5 text-sm text-ink placeholder:text-faint transition-colors duration-200 ease-snappy",
                url.length > 0 && !urlValid ? "border-bad" : "border-line hover:border-line-strong",
                busy && "pointer-events-none opacity-50",
              )}
            />
          </div>
          {url.length > 0 && !urlValid ? (
            <p className="mt-2 text-sm text-bad">{t("youtubeInvalid")}</p>
          ) : (
            <p className="mt-2 text-xs leading-relaxed text-faint">{t("youtubeHint")}</p>
          )}
        </div>
      ) : null}

      {/* Both settings are compact, same weight — one grouped row (not two full-width stacked
          sections) reads as one settings cluster instead of a longer scanning list. */}
      <div className="mt-5 grid grid-cols-1 gap-5 sm:grid-cols-2">
        <div>
          <Eyebrow tone="faint">{t("clipCount")}</Eyebrow>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            {/* Auto («сколько найдётся, до 30») vs Custom (ровно N). */}
            <div className="inline-flex rounded-md border border-line bg-surface p-0.5">
              {([true, false] as const).map((isAuto) => (
                <button
                  key={String(isAuto)}
                  type="button"
                  onClick={() => setAuto(isAuto)}
                  disabled={busy}
                  aria-pressed={auto === isAuto}
                  className={cn(
                    "rounded-sm px-3.5 py-1.5 text-sm font-medium transition disabled:opacity-50",
                    auto === isAuto
                      ? "bg-surface-3 text-ink shadow-[0_0_0_1px_var(--color-line-strong)]"
                      : "text-muted hover:text-ink",
                  )}
                >
                  {isAuto ? t("auto") : t("custom")}
                </button>
              ))}
            </div>
            {!auto && (
              <div className="inline-flex items-center gap-1 rounded-md border border-line bg-surface p-1">
                <IconButton
                  onClick={() => setCount((c) => clamp(c - 1))}
                  disabled={busy || count <= MIN_CLIPS}
                  aria-label={t("fewerClips")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Minus className="size-4" />
                </IconButton>
                <Numeral className="w-7 text-center text-base font-semibold text-ink">{count}</Numeral>
                <IconButton
                  onClick={() => setCount((c) => clamp(c + 1))}
                  disabled={busy || count >= MAX_CLIPS}
                  aria-label={t("moreClips")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Plus className="size-4" />
                </IconButton>
              </div>
            )}
            <span className="font-mono text-eyebrow uppercase tabular-nums text-faint">
              {auto ? t("autoHint", { max: MAX_CLIPS }) : t("customHint", { count })}
            </span>
          </div>
        </div>

        <div>
          <Eyebrow tone="faint">{t("language")}</Eyebrow>
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              role="switch"
              aria-checked={kazakh}
              onClick={() => setKazakh((k) => !k)}
              disabled={busy}
              aria-label={t("kazakhAria")}
              className={cn(
                "relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200 ease-snappy focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                kazakh ? "bg-accent" : "bg-surface-3",
                busy && "opacity-50",
              )}
            >
              <span
                className={cn(
                  "inline-block size-5 rounded-full bg-white shadow-sm transition-transform duration-200 ease-snappy",
                  kazakh ? "translate-x-4" : "translate-x-0",
                )}
              />
            </button>
            <span className="text-sm text-ink">{t("kazakh")}</span>
            <span className="font-mono text-eyebrow uppercase tabular-nums text-faint">
              {kazakh ? t("kazakhOn") : t("kazakhOff")}
            </span>
          </div>
        </div>

        <div className="sm:col-span-2">
          <Eyebrow tone="faint">{t("clipDuration")}</Eyebrow>
          <div className="mt-2 flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted">{t("minClip")}</span>
              <div className="inline-flex items-center gap-1 rounded-md border border-line bg-surface p-1">
                <IconButton
                  onClick={() => applyMin(clipMinSec - 2)}
                  disabled={busy || clipMinSec <= MIN_CLIP_SEC}
                  aria-label={t("shorterMinClip")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Minus className="size-4" />
                </IconButton>
                <div className="flex items-center">
                  <input
                    type="text"
                    inputMode="numeric"
                    value={minDraft}
                    onChange={(e) => setMinDraft(e.target.value.replace(/\D/g, "").slice(0, 3))}
                    onBlur={() => applyMin(parseInt(minDraft, 10) || clipMinSec)}
                    disabled={busy}
                    aria-label={t("minClip")}
                    className="w-8 bg-transparent text-center font-mono text-sm font-semibold tabular-nums text-ink outline-none disabled:opacity-50"
                  />
                  <span className="text-sm text-faint">s</span>
                </div>
                <IconButton
                  onClick={() => applyMin(clipMinSec + 2)}
                  disabled={busy || clipMinSec >= clipMaxSec - 2}
                  aria-label={t("longerMinClip")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Plus className="size-4" />
                </IconButton>
              </div>
            </div>
            <span className="text-sm text-faint">-</span>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted">{t("maxClip")}</span>
              <div className="inline-flex items-center gap-1 rounded-md border border-line bg-surface p-1">
                <IconButton
                  onClick={() => applyMax(clipMaxSec - 2)}
                  disabled={busy || clipMaxSec <= Math.max(MIN_CLIP_MAX_SEC, clipMinSec + 2)}
                  aria-label={t("shorterMaxClip")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Minus className="size-4" />
                </IconButton>
                <div className="flex items-center">
                  <input
                    type="text"
                    inputMode="numeric"
                    value={maxDraft}
                    onChange={(e) => setMaxDraft(e.target.value.replace(/\D/g, "").slice(0, 3))}
                    onBlur={() => applyMax(parseInt(maxDraft, 10) || clipMaxSec)}
                    disabled={busy}
                    aria-label={t("maxClip")}
                    className="w-8 bg-transparent text-center font-mono text-sm font-semibold tabular-nums text-ink outline-none disabled:opacity-50"
                  />
                  <span className="text-sm text-faint">s</span>
                </div>
                <IconButton
                  onClick={() => applyMax(clipMaxSec + 2)}
                  disabled={busy || clipMaxSec >= MAX_CLIP_SEC}
                  aria-label={t("longerMaxClip")}
                  size="sm"
                  className="size-9 text-ink hover:text-ink sm:size-7"
                >
                  <Plus className="size-4" />
                </IconButton>
              </div>
            </div>
          </div>
        </div>
      </div>

      {videoTooShort && (
        <p className="mt-4 text-sm text-warn">
          {t("videoTooShort", { dur: Math.round(fileDuration ?? 0), min: clipMinSec })}
        </p>
      )}

      <Button
        type="submit"
        variant="accent"
        size="lg"
        loading={busy}
        disabled={!canSubmit}
        className="mt-6 h-12 w-full sm:w-auto"
      >
        {!busy && <Scissors className="size-5" />}
        {busy ? t("starting") : t("makeClips")}
      </Button>

      <p className="mt-3 text-sm text-muted">{t("creditNote")}</p>
    </form>
  );
}
