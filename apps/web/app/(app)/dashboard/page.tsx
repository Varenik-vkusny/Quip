"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { AppHeader } from "@/components/app/AppHeader";
import { PromoRedeem } from "@/components/app/PromoRedeem";
import { RecentProjects } from "@/components/app/RecentProjects";
import { UsageMeter } from "@/components/app/UsageMeter";
import { ErrorPanel } from "@/components/ErrorPanel";
import { JobProgress } from "@/components/JobProgress";
import { ReconnectBanner } from "@/components/ReconnectBanner";
import { SourceForm } from "@/components/SourceForm";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { Spinner } from "@/components/ui/Spinner";
import { Split } from "@/components/ui/Split";
import { ResultTabs } from "@/components/videomap/ResultTabs";
import {
  cancelJob,
  clearUploadCheckpoint,
  createJob,
  createUploadJob,
  type UploadProgressInfo,
} from "@/lib/api";
import { etaSeconds, formatRate } from "@/lib/format";
import { markReviewed, noteRecentCreated } from "@/lib/recent";
import { createSupabaseBrowserClient } from "@/lib/supabase/client";
import { isSupabaseConfigured } from "@/lib/supabase/config";
import { useJob } from "@/lib/useJob";
import { useUsage } from "@/lib/useUsage";

// Heavy, phase-gated result views — NOT shown on the idle "New project" first paint (the only
// thing a returning user sees before they submit). Code-split so their JS (and their transitive
// deps: ClipCard/ExportMenu, the video-map narrative parser + polling) stays out of the initial
// dashboard bundle and only loads once a job actually reaches a state that renders them. ssr:false
// is safe here — the dashboard is an authed client route (no SEO), and both already run client-only.
// A light skeleton keeps the swap from flashing blank while the chunk streams in.

/** Placeholder for the results grid while its chunk loads (mirrors ClipGrid's 3-col card layout). */
function ClipGridSkeleton() {
  return (
    <div className="w-full" aria-hidden>
      <div className="mb-5 h-8 w-56 rounded bg-surface-2 motion-safe:animate-pulse" />
      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="aspect-[9/16] rounded-lg bg-surface-2 motion-safe:animate-pulse"
          />
        ))}
      </div>
    </div>
  );
}

/** Placeholder for the video-map card while its chunk loads. */
function VideoMapSkeleton() {
  return (
    <div className="mb-6 rounded-lg border border-line bg-surface p-5" aria-hidden>
      <div className="h-3.5 w-24 rounded bg-surface-2 motion-safe:animate-pulse" />
      <div className="mt-3 h-6 w-2/3 rounded bg-surface-2 motion-safe:animate-pulse" />
    </div>
  );
}

const ClipGrid = dynamic(() => import("@/components/ClipGrid").then((m) => m.ClipGrid), {
  ssr: false,
  loading: () => <ClipGridSkeleton />,
});
const ResultsVideoMap = dynamic(
  () => import("@/components/videomap/ResultsVideoMap").then((m) => m.ResultsVideoMap),
  { ssr: false, loading: () => <VideoMapSkeleton /> },
);

/** Friendly label for the recent-projects list (no PII, just a hint). */
function labelFromUrl(url: string): string {
  try {
    const u = new URL(url);
    const v = u.searchParams.get("v");
    if (v) return `YouTube · ${v}`;
    return u.hostname.replace(/^www\./, "") + u.pathname.slice(0, 22);
  } catch {
    return url.slice(0, 40) || "Project";
  }
}

/** Shown the instant a file upload starts (before bytes even move) so a big upload
 *  never looks frozen. Real % comes from XHR upload-progress events. Leads with a big
 *  mono % readout + a determinate bar while bytes move.
 *
 *  At 100% the bytes are up but the worker is still finalizing the multipart upload + creating the
 *  job (the `upload-complete` round-trip) BEFORE status polling can begin — a window with no server
 *  progress to show. A full, static bar there reads as frozen (the L5 "silent gap"), so we flip to
 *  an explicit, animated "Preparing your video…" state (spinner + indeterminate bar) until
 *  `createUploadJob` resolves and the live status view (the JobProgress stepper) takes over. */
function UploadProgress({ info }: { info: UploadProgressInfo }) {
  const t = useTranslations("dashboard");
  const { pct, loadedBytes, totalBytes, elapsedSec } = info;
  const preparing = pct >= 100;

  // Live browser→R2 speed + ETA — pure client math from the bytes + elapsedSec we thread through the
  // upload callback (elapsed is measured at event time in lib/api, so render stays pure: no Date.now()
  // here). Only while bytes are actually moving (not in the "preparing" finalize window). Degrades
  // gracefully: instant/tiny uploads never build enough signal, so formatRate/etaSeconds return null
  // and we quietly fall back to the plain hint.
  const rateLabel = preparing || elapsedSec <= 0 ? null : formatRate(loadedBytes / elapsedSec);
  const etaSec = preparing ? null : etaSeconds(elapsedSec, totalBytes > 0 ? loadedBytes / totalBytes : 0);
  const etaLabel =
    etaSec == null
      ? null
      : etaSec < 60
        ? t("uploadEtaSeconds", { value: etaSec })
        : t("uploadEtaMinutes", { value: Math.round(etaSec / 60) });
  const rateEtaLine = [rateLabel, etaLabel].filter(Boolean).join(" · ") || null;

  // Non-fatal recovery narration: a part timed out / stalled / errored and the client is retrying it
  // with backoff (the upload is NOT dead — it keeps going once the network recovers). Honest, calm,
  // and distinct from the red ErrorPanel (which only appears after retries are exhausted).
  const retrying = info.phase === "retrying" || info.phase === "stalled";
  const retryMsg = !retrying
    ? null
    : t(info.phase === "stalled" ? "uploadStalled" : "uploadRetrying");

  return (
    <div className="mx-auto w-full max-w-2xl">
      <div className="flex items-end justify-between gap-4 border-b border-line pb-4">
        <div>
          <Eyebrow tone="accent">{preparing ? t("preparing") : t("uploading")}</Eyebrow>
          <h2 className="mt-1.5 font-display text-h3 text-ink">
            {preparing ? t("preparingVideo") : t("sendingVideo")}
          </h2>
        </div>
        <div className="flex items-center justify-end text-right" aria-live="polite">
          {preparing ? (
            <Spinner size="lg" className="text-accent" />
          ) : (
            <div>
              <Numeral className="block text-display-lg font-semibold leading-none text-ink">
                {pct}
              </Numeral>
              <Eyebrow tone="faint" className="mt-1 block">
                {t("percent")}
              </Eyebrow>
            </div>
          )}
        </div>
      </div>
      <div className="mt-5 h-1 overflow-hidden rounded-pill bg-surface-3">
        <div
          className={
            preparing
              ? "h-full w-full rounded-pill bg-accent motion-safe:animate-pulse"
              : "h-full rounded-pill bg-accent transition-[width] duration-200 ease-out"
          }
          style={preparing ? undefined : { width: `${Math.max(2, pct)}%` }}
        />
      </div>
      {retryMsg && (
        <div
          className="mt-3 flex items-center gap-2 rounded-md border border-line bg-[color-mix(in_srgb,var(--t-warn)_8%,transparent)] px-3 py-2"
          aria-live="polite"
        >
          <Spinner size="sm" className="text-warn" />
          <span className="text-xs text-warn">{retryMsg}</span>
        </div>
      )}
      {rateEtaLine && (
        <p className="mt-3 text-xs tabular-nums text-muted" aria-live="polite">
          {rateEtaLine}
        </p>
      )}
      <p className={`${rateEtaLine ? "mt-1.5" : "mt-3"} text-xs leading-relaxed text-faint`}>
        {preparing ? t("uploadHintPreparing") : t("uploadHintSending")}
      </p>
    </div>
  );
}

function DashboardInner() {
  const t = useTranslations("dashboard");
  const router = useRouter();
  const { job, jobId, error: pollError, reconnecting, elapsed, start, reset, reconnectNow } = useJob();
  const { usage } = useUsage(); // owner plan → drives the Video Map's Extract gate
  const [submitting, setSubmitting] = useState(false);
  const [upload, setUpload] = useState<UploadProgressInfo | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // The last file upload we attempted — retained through the error screen so "Resume" can re-enter
  // createUploadJob (which short-circuits to the remaining parts via the IndexedDB checkpoint) without
  // making the user re-pick a multi-GB file. Cleared on success and on a clean "Start over".
  const [pendingUpload, setPendingUpload] = useState<{
    file: File;
    maxClips: number;
    language: string | null;
    clipMinSec?: number;
    clipMaxSec?: number;
    removePauses?: boolean;
  } | null>(null);
  // Requested clip count (from the form) → drives the reserved skeleton count while processing.
  const [requestedClips, setRequestedClips] = useState(3);
  const error = submitError ?? pollError;
  // In-flight upload controller — abort on reset / unmount / new submit. Без этого брошенная
  // загрузка (ушёл со страницы / «Новый проект» / залил второй раз) продолжала XHR: создавала
  // лишний джоб на воркере, а её поздний resolve перетирал UI (дубли джоб, скачущий прогресс).
  const uploadCtrl = useRef<AbortController | null>(null);
  useEffect(() => () => uploadCtrl.current?.abort(), []); // unmount → отменить загрузку

  // Current user id → per-user "reviewed" badge namespacing (null in dev/no-auth). Resolved once.
  const [userId, setUserId] = useState<string | null>(null);
  useEffect(() => {
    if (!isSupabaseConfigured) return;
    let active = true;
    createSupabaseBrowserClient()
      .auth.getUser()
      .then(({ data }) => {
        if (active) setUserId(data.user?.id ?? null);
      })
      .catch(() => {
        /* keep null — reviewed flag just won't persist; not fatal */
      });
    return () => {
      active = false;
    };
  }, []);

  // Deep-link: open an existing job via ?job=<id>. Reactive to the query param so
  // navigating from /dashboard to /dashboard?job=X (e.g. clicking a recent project
  // while already on the dashboard) loads it immediately — no manual refresh. The
  // mount-only effect missed this: same route + new query doesn't remount the page.
  const jobParam = useSearchParams().get("job");
  useEffect(() => {
    if (jobParam) start(jobParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobParam]);

  // Mark a job reviewed (per-user) once its results are on screen → clears the "New" badge in the
  // recents list. The list is server-authoritative now (getMyJobs returns live status/nclips), so
  // there's no client-side status cache to keep in sync. userId in deps so this fires once it resolves.
  useEffect(() => {
    if (job?.status === "done") markReviewed(userId, job.id);
  }, [job?.id, job?.status, userId]);

  async function handleSubmit(url: string, maxClips: number, clipMinSec?: number, clipMaxSec?: number) {
    if (submitting) return; // guard: не плодим параллельные джобы двойным сабмитом
    setSubmitError(null);
    setSubmitting(true);
    setRequestedClips(maxClips);
    try {
      const { id } = await createJob({ source_type: "youtube", source_ref: url, max_clips: maxClips, clip_min_sec: clipMinSec, clip_max_sec: clipMaxSec });
      // Optimistic recents entry + refetch signal → the new project shows instantly in the list
      // (and RecentProjects refetches getMyJobs) before the server list catches up.
      noteRecentCreated({ id, label: labelFromUrl(url), at: Date.now() });
      start(id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : t("errors.create"));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSubmitFile(file: File, maxClips: number, language: string | null = null, clipMinSec?: number, clipMaxSec?: number, removePauses?: boolean) {
    if (submitting) return; // guard двойного сабмита (форма и так дизейблится — belt+braces)
    uploadCtrl.current?.abort(); // отменить прежнюю незавершённую загрузку перед новой
    const ctrl = new AbortController();
    uploadCtrl.current = ctrl;
    setSubmitError(null);
    setSubmitting(true);
    setRequestedClips(maxClips);
    // Show the uploading screen immediately, before bytes move (0%). Real bytes + a real elapsedSec
    // (measured at event time in lib/api) overwrite this on the first progress event.
    setUpload({ pct: 0, loadedBytes: 0, totalBytes: file.size, elapsedSec: 0 });
    // Remember this attempt so the error screen can offer "Resume" (checkpoint-backed).
    setPendingUpload({ file, maxClips, language, clipMinSec, clipMaxSec, removePauses });
    try {
      const { id } = await createUploadJob(file, maxClips, setUpload, ctrl.signal, language, clipMinSec, clipMaxSec, undefined, removePauses);
      if (ctrl.signal.aborted) return; // отменена/вытеснена — не трогаем стейт
      setUpload(null);
      setPendingUpload(null); // fully uploaded → nothing to resume
      noteRecentCreated({ id, label: file.name, at: Date.now() });
      start(id);
    } catch (e) {
      // намеренная отмена (reset/unmount/новый сабмит) → НЕ показываем ошибку
      if (ctrl.signal.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      setUpload(null);
      // Keep pendingUpload so the ErrorPanel's "Resume" can pick the transfer back up.
      setSubmitError(e instanceof Error ? e.message : t("errors.upload"));
    } finally {
      if (uploadCtrl.current === ctrl) {
        uploadCtrl.current = null;
        setSubmitting(false);
      }
    }
  }

  // Resume an interrupted upload: re-enter createUploadJob with the SAME file. The IndexedDB
  // checkpoint short-circuits it to only the parts still missing (no restart from 0%).
  function handleResume() {
    const p = pendingUpload;
    if (!p) return;
    void handleSubmitFile(p.file, p.maxClips, p.language, p.clipMinSec, p.clipMaxSec, p.removePauses);
  }

  async function handleStop() {
    if (!jobId) return;
    try {
      await cancelJob(jobId);
    } catch (e) {
      // 409 (already in a paid stage) or network error → surface, don't reset (job keeps going).
      setSubmitError(e instanceof Error ? e.message : t("errors.stop"));
      return;
    }
    handleReset(); // back to idle; the recent-projects entry is kept (added at submit time)
  }

  function handleReset() {
    uploadCtrl.current?.abort(); // отменить незавершённую загрузку — иначе она позже войдёт в tracking
    uploadCtrl.current = null;
    // Start over means "forget the interrupted transfer": drop its resume checkpoint so re-uploading
    // the same file begins fresh (not resumes) — the deliberate clean-slate path.
    if (pendingUpload) void clearUploadCheckpoint(pendingUpload.file);
    setPendingUpload(null);
    reset();
    setSubmitError(null);
    setSubmitting(false);
    setUpload(null);
    // Drop ?job= so a deep-linked project doesn't keep us out of the idle "New project"
    // form (phase below treats a present ?job= as loading). Without this, reset couldn't
    // return to idle when the URL still carried the job id.
    if (jobParam) router.replace("/dashboard");
  }

  // `jobId` (set the moment a job starts) keeps us in "tracking" through the gap between submit
  // and the first status poll — otherwise the dashboard flashed back for a beat (worst on a Modal
  // cold start) before the first poll populated `job`.
  // `jobParam`: a deep-link (e.g. editor's "All clips" → /dashboard?job=X) must NOT flash the idle
  // "Create clips" form before the load effect runs. Treating a present ?job= as loading keeps us
  // out of idle until the job resolves to its grid (done) — fixes "All clips lands on the dashboard".
  const phase = error
    ? "error"
    : job?.status === "done"
      ? "done"
      : job?.status === "cancelled"
        ? "cancelled" // user stopped it (or deep-linked a stopped job) → neutral panel, not a loader
        : reconnecting
          ? "reconnecting" // a connectivity blip — calm banner, NOT the red error panel; job's alive
          : submitting || jobId || jobParam
            ? "tracking"
            : "idle";
  // Opening an existing project via deep-link, before the first poll populates `job`: show a neutral
  // loader (not the JobProgress stepper, which implies "still processing" for an already-done clip).
  const openingProject = !!jobParam && !job && !submitting;
  // Progressive results: the worker populates job.clips (metadata for ALL clips) while still
  // rendering — clips with a video_url are ready, the rest carry empty video_url. The moment any
  // clip exists we swap the bare stepper for the grid so the user can read hooks/reasons and start
  // editing ready clips while the rest finish. Early phases (queued…selecting) have no clips yet →
  // the stepper still shows.
  const showProgressiveGrid = phase === "tracking" && !openingProject && (job?.clips?.length ?? 0) > 0;

  return (
    <div className="min-h-dvh">
      <AppHeader />
      <main className="mx-auto max-w-[1200px] px-5 py-10 sm:px-8 sm:py-12">
        {phase !== "idle" && (
          <button
            onClick={handleReset}
            className="mb-6 inline-flex items-center gap-1.5 rounded-md border border-line px-3 py-1.5 text-sm text-muted transition hover:border-line-strong hover:text-ink"
          >
            <Plus className="size-4" />
            {t("newProject")}
          </button>
        )}

        {phase === "idle" ? (
          // Instrument console: an intake protagonist on the left, a calibrated readout rail
          // on the right (unequal weight). Stacks on mobile via Split.
          <Split variant="main-rail">
            <section>
              <Eyebrow tone="faint">{t("newProject")}</Eyebrow>
              <h1 className="mt-2 font-display text-h2 text-ink sm:text-display-lg">
                {t("createClips")}
              </h1>
              <p className="mt-3 max-w-md text-lead text-muted">{t("intro")}</p>
              <div className="mt-8">
                <SourceForm onSubmit={handleSubmit} onSubmitFile={handleSubmitFile} busy={submitting} />
              </div>
            </section>
            {/* readout rail — meter (primary), recents (secondary), promo (tertiary) */}
            <aside className="space-y-5">
              <UsageMeter />
              <RecentProjects />
              <PromoRedeem />
            </aside>
          </Split>
        ) : upload !== null ? (
          <UploadProgress info={upload} />
        ) : showProgressiveGrid && job ? (
          // Progressive grid: clips render in as they finish. Same <ClipGrid key={job.id}> as the
          // done branch → flipping to "done" doesn't remount (no flicker, selection preserved).
          <ClipGrid key={job.id} job={job} />
        ) : phase === "reconnecting" ? (
          // Connectivity blip: the job keeps processing server-side, so we show a calm reconnect
          // banner (NOT the red ErrorPanel) and keep any clips that already arrived usable below.
          (job?.clips?.length ?? 0) > 0 && job ? (
            <>
              <ReconnectBanner onRetry={reconnectNow} />
              <div className="mt-6">
                <ClipGrid key={job.id} job={job} />
              </div>
            </>
          ) : (
            <ReconnectBanner onRetry={reconnectNow} />
          )
        ) : phase === "tracking" ? (
          // Centered (mx-auto on each panel) so the transient upload/processing screens don't sit
          // jammed against the left edge with a dead right half (founder call). Idle + done fill width.
          openingProject ? (
            <div className="flex items-center justify-center gap-3 py-10">
              <Spinner size="md" className="text-accent" />
              <p className="text-sm text-muted">{t("openingProject")}</p>
            </div>
          ) : (
            <JobProgress
              status={job?.status ?? "queued"}
              elapsed={elapsed}
              progress={job?.progress ?? null}
              cancellable={job?.cancellable ?? false}
              onStop={handleStop}
              sourceMinutes={job?.source_minutes ?? null}
              transcriptWords={job?.transcript_words ?? null}
              momentsFound={job?.moments_found ?? null}
              requestedClips={requestedClips}
              sourceKind={job?.source_kind ?? null}
            />
          )
        ) : phase === "done" && job ? (
          <ResultTabs
            clipCount={job.clips?.length ?? 0}
            clips={<ClipGrid key={job.id} job={job} />}
            map={
              <ResultsVideoMap jobId={job.id} clips={job.clips ?? []} plan={usage?.plan} />
            }
          />
        ) : phase === "cancelled" ? (
          <div className="mx-auto max-w-xl">
            <Eyebrow tone="faint">{t("runStopped")}</Eyebrow>
            <h2 className="mt-2 font-display text-h3 text-ink">{t("projectStopped")}</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">{t("projectStoppedBody")}</p>
          </div>
        ) : phase === "error" && error ? (
          // A resumable upload error → Resume (primary) + Start over (secondary). Any other error
          // (YouTube import / status poll) → a single "Try again" that resets.
          <ErrorPanel
            message={error}
            onRetry={handleReset}
            retryLabel={pendingUpload ? t("startOver") : t("tryAgain")}
            onResume={pendingUpload ? handleResume : undefined}
            resumeLabel={t("resumeUpload")}
            eyebrow={t("runStopped")}
            title={t("somethingWrong")}
          />
        ) : null}
      </main>
    </div>
  );
}

// useSearchParams() requires a Suspense boundary (Next App Router). The shell renders
// instantly; the tool hydrates inside.
export default function DashboardPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-dvh">
          <AppHeader />
        </div>
      }
    >
      <DashboardInner />
    </Suspense>
  );
}
