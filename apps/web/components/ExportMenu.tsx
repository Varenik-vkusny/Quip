"use client";

import {
  Captions,
  ChevronDown,
  Clapperboard,
  Download,
  FileText,
  Film,
  Loader2,
  Package,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  captionedDownloadUrl,
  clipCleanExportUrl,
  clipSrtUrl,
  getAccessToken,
  getMediaToken,
  withToken,
} from "@/lib/api";
import { DownloadHttpError } from "@/lib/bulkDownload";
import { ConfirmRenderModal } from "@/components/editor/ConfirmRenderModal";

// Меню «Скачать» — экспорт-свобода: юзер уносит клип в любой редактор.
//  • С субтитрами (MP4)  — прожжённый клип. Если есть СВЕЖИЙ рендер (bakedUrl) — отдаём его
//    (быстро); иначе рендерим на лету из ТЕКУЩЕГО edit-state (всегда совпадает с превью).
//  • Без субтитров (MP4) — чистая 9:16-вертикалка (рендер на лету, текущий edit-state).
//  • Субтитры (.SRT)     — тайминги совпадают с видео, импорт в CapCut/Premiere/Resolve.
//
// FEEDBACK: «С/без субтитрами» рендерятся на лету (десятки секунд) — раньше это были голые
// `<a download>`, и пока сервер рендерил, НИЧЕГО не происходило → юзер кликал по 3 раза. Теперь
// клик качает через fetch + показывает спиннер «Preparing…» и блокирует пункт, пока файл не готов;
// при ошибке (CORS/сеть) — фолбэк на прямую ссылку. Имя файла — из Content-Disposition, иначе дефолт.

const WORKER_BASE = process.env.NEXT_PUBLIC_WORKER_URL ?? "";

/** Fetch a (possibly on-demand-rendered) export as a blob and save it to disk. Filename comes
 *  from Content-Disposition when present, else `fallbackName`. Exported so the grid's "Download
 *  all" reuses the exact same blob-download path (no duplicated logic). Throws `DownloadHttpError`
 *  on a non-OK response — carrying the STATUS and `Retry-After`, so the caller can tell "the server
 *  is busy, wait 5s" (503) apart from a real failure. It used to throw a bare `Error("HTTP 503")`,
 *  which made a retryable "busy" look identical to a hard error, and callers gave up (no silent
 *  swallow, but no useful signal either).
 *
 *  `auth: false` sends NO Authorization header — required for PUBLIC CDN objects: an Authorization
 *  header turns a simple cross-origin GET into a PREFLIGHTED one, and R2 need not answer OPTIONS. */
export async function downloadFile(
  url: string,
  fallbackName: string,
  opts: { auth?: boolean } = {},
): Promise<void> {
  // On-demand export endpoints live on the worker and are ownership-gated → send the bearer token
  // (fetched fresh so it's always current).
  const token = opts.auth === false ? null : await getAccessToken();
  const res = await fetch(url, token ? { headers: { Authorization: `Bearer ${token}` } } : undefined);
  if (!res.ok) throw new DownloadHttpError(res.status, res.headers.get("retry-after"));
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition");
  const name = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd ?? "")?.[1] ?? fallbackName;
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objUrl;
  a.download = decodeURIComponent(name);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objUrl), 10_000);
}

export function ExportMenu({
  jobId,
  clipId,
  bakedUrl = null,
  cleanBakedUrl = null,
  dirty = false,
  pixelDirty = false,
  onRender,
  align = "right",
  placement = "down",
  className = "",
}: {
  jobId: string;
  clipId: string;
  /** Свежий прожжённый рендер (render_url после «Рендер»). null → рендерим на лету. */
  bakedUrl?: string | null;
  /** Stable CLEAN (no-caption) clip already on the CDN — `clip.video_url` from the grid. When
   *  present, "No captions" streams THIS instantly instead of re-rendering `export/clean.mp4`.
   *  The editor leaves it null (edits may diverge from the baked clean clip) → on-demand render. */
  cleanBakedUrl?: string | null;
  /** Есть НЕ-отрендеренные правки (изменён шрифт хука и т.п.)? → bakedUrl устарел,
   *  скачиваем свежий on-demand рендер, а не протухший CDN-снимок. */
  dirty?: boolean;
  /** Pixel-level edits (framing/crop/aspect/trim/motion) since last render — the downloaded
   *  file has wrong pixels. When true, captioned/clean download shows a re-render confirm modal. */
  pixelDirty?: boolean;
  /** Trigger a full re-render (same as the Render button). Called when user picks "Re-render now"
   *  in the ConfirmRenderModal. Required when pixelDirty can be true (editor context). */
  onRender?: () => void;
  align?: "left" | "right";
  placement?: "up" | "down";
  className?: string;
}) {
  const t = useTranslations("exportMenu");
  const [open, setOpen] = useState(false);
  // Which item is currently being prepared/downloaded (its key), or null. Disables clicks + spins.
  const [busy, setBusy] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  // ── ConfirmRenderModal state (pixelDirty gate) ──
  // confirmOpen: modal is visible. pendingRef: the download that was intercepted by the modal.
  // awaitingRenderRef: user picked "Re-render now" — when pixelDirty clears we auto-download.
  // A ref (not state) so the effect only re-runs on pixelDirty / url changes, not on this flag.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const pendingRef = useRef<{ key: string; fallbackName: string } | null>(null);
  const awaitingRenderRef = useRef(false);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // No captions: prefer the stable clean CDN clip (instant) when the caller has it; else render
  // a fresh clean export on demand.
  const cleanInstant = !!cleanBakedUrl;
  const cleanUrl = cleanBakedUrl ?? clipCleanExportUrl(WORKER_BASE, jobId, clipId);
  const srtUrl = clipSrtUrl(WORKER_BASE, jobId, clipId);
  // "For your editor" handoff exports — all render on the fly (text/zip), so they use the
  // fetch+blob path (instant=false) which sends the Authorization header; the ?mt= fallback in
  // `pick` covers surfaces that can't. Project-scoped (EDL/FCPXML/markers/package) need only jobId;
  // captions (.vtt) is per-clip. URLs inlined here (worker origin + ids) — same contract as the
  // per-format worker endpoints.
  const packageUrl = `${WORKER_BASE}/jobs/${jobId}/export/package.zip`;
  const edlUrl = `${WORKER_BASE}/jobs/${jobId}/export.edl`;
  const fcpxmlUrl = `${WORKER_BASE}/jobs/${jobId}/export.fcpxml`;
  // С субтитрами: baked-рендер ТОЛЬКО когда нет невыданных правок (dirty=false). При dirty
  // (юзер сменил шрифт хука и т.п.) baked устарел → on-demand рендер текущего edit-state.
  const captionedUrl = captionedDownloadUrl(WORKER_BASE, jobId, clipId, bakedUrl, dirty);

  // A baked CDN render is instant (no server render) → don't bother with the fetch path. The on-demand
  // endpoints render on the fly → fetch + spinner so the user sees progress instead of dead-clicking.
  const captionedInstant = !!bakedUrl && !dirty;

  // After "Re-render now": once pixelDirty clears (render completed), trigger the queued download
  // with the fresh post-render urls (bakedUrl will be set, captionedInstant will be true).
  // awaitingRenderRef is a ref — we read it here but don't put it in deps; the effect re-runs
  // only when pixelDirty / urls change, which is exactly when we need to act.
  useEffect(() => {
    if (!awaitingRenderRef.current || pixelDirty) return;
    awaitingRenderRef.current = false; // consume the flag (ref mutation — no re-render)
    const p = pendingRef.current;
    pendingRef.current = null;
    if (!p) return;
    const url = p.key === "captioned" ? captionedUrl : cleanUrl;
    const instant = p.key === "captioned" ? captionedInstant : cleanInstant;
    if (instant) {
      const a = document.createElement("a");
      a.href = url;
      a.download = p.fallbackName;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else {
      setBusy(p.key);
      void downloadFile(url, p.fallbackName)
        .then(() => setBusy(null))
        .catch(async (e) => {
          console.warn("[export] post-render blob download failed, falling back:", e);
          setBusy(null);
          // Fallback nav can't set a header → append a scoped media ticket (?mt=). If null, navigate
          // without it (the primary fetch path already sends the Authorization header in the normal case).
          window.location.href = withToken(url, await getMediaToken(jobId), "mt");
        });
    }
  }, [pixelDirty, captionedUrl, cleanUrl, captionedInstant, cleanInstant, jobId]);

  const pick = async (key: string, url: string, fallbackName: string, instant: boolean) => {
    if (busy) return;
    // Gate: if there are unrendered pixel edits (framing/crop/aspect/trim/motion), intercept
    // captioned and clean downloads and show the ConfirmRenderModal instead.
    if (pixelDirty && (key === "captioned" || key === "clean")) {
      pendingRef.current = { key, fallbackName };
      setConfirmOpen(true);
      return;
    }
    if (instant) {
      // Stable CDN artifact — let the browser stream it to disk natively (no render wait).
      const a = document.createElement("a");
      a.href = url;
      a.download = fallbackName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setOpen(false);
      return;
    }
    setBusy(key);
    try {
      await downloadFile(url, fallbackName);
      setOpen(false);
    } catch (e) {
      // CORS/network → fall back to a direct navigation (native download, no progress UI).
      console.warn("[export] blob download failed, falling back to direct link:", e);
      window.location.href = withToken(url, await getMediaToken(jobId), "mt");
    } finally {
      setBusy(null);
    }
  };

  // Modal: "Re-render now" — trigger a full render; the useEffect above auto-downloads when done.
  const handleRerender = () => {
    setConfirmOpen(false);
    setOpen(false);
    awaitingRenderRef.current = true; // arm the auto-download; effect fires when pixelDirty clears
    onRender?.();
  };

  // Modal: "Download anyway" — proceed with the current on-demand download, bypassing the gate.
  const handleDownloadAnyway = () => {
    setConfirmOpen(false);
    const p = pendingRef.current;
    pendingRef.current = null;
    if (!p) return;
    // Resolve urls from current props (pixelDirty is still true → on-demand endpoint).
    const url = p.key === "captioned" ? captionedUrl : cleanUrl;
    const instant = p.key === "captioned" ? captionedInstant : cleanInstant;
    if (busy) return;
    if (instant) {
      const a = document.createElement("a");
      a.href = url;
      a.download = p.fallbackName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setOpen(false);
      return;
    }
    setBusy(p.key);
    void downloadFile(url, p.fallbackName)
      .then(() => { setBusy(null); setOpen(false); })
      .catch(async (e) => {
        console.warn("[export] blob download failed, falling back to direct link:", e);
        setBusy(null);
        window.location.href = withToken(url, await getMediaToken(jobId), "mt");
      });
  };

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="inline-flex w-full items-center justify-center gap-1.5 rounded-sm border border-line bg-surface-2 px-3 py-2 text-sm font-semibold text-ink transition duration-200 ease-snappy hover:-translate-y-px hover:border-line-strong hover:bg-surface-3"
      >
        <Download className="size-4" />
        {t("download")}
        <ChevronDown className={`size-3.5 transition ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute z-30 min-w-[238px] max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-md border border-line-strong bg-surface shadow-pop ${
            align === "right" ? "right-0" : "left-0"
          } ${placement === "up" ? "bottom-full mb-1" : "mt-1"}`}
        >
          <ExportItem
            icon={<Captions className="size-4" />}
            title={t("captioned.title")}
            sub={captionedInstant ? t("captioned.subRendered") : t("captioned.subRender")}
            busy={busy === "captioned"}
            busyLabel={t("preparingClip")}
            disabled={!!busy && busy !== "captioned"}
            onPick={() => pick("captioned", captionedUrl, `${clipId}-captioned.mp4`, captionedInstant)}
          />
          <ExportItem
            icon={<Film className="size-4" />}
            title={t("clean.title")}
            sub={cleanInstant ? t("clean.subInstant") : t("clean.subRender")}
            busy={busy === "clean"}
            busyLabel={t("preparingClip")}
            disabled={!!busy && busy !== "clean"}
            onPick={() => pick("clean", cleanUrl, `${clipId}-clean.mp4`, cleanInstant)}
          />
          <ExportItem
            icon={<FileText className="size-4" />}
            title={t("srt.title")}
            sub={t("srt.sub")}
            busy={busy === "srt"}
            busyLabel={t("preparing")}
            disabled={!!busy && busy !== "srt"}
            onPick={() => pick("srt", srtUrl, `${clipId}.srt`, false)}
          />
          {/* ── "For your editor": hand off to a pro NLE (Premiere / Resolve / Final Cut / CapCut).
                English labels are fine (editor UI is on the deferred-i18n list). ── */}
          <div className="border-t border-line" />
          <p className="px-3 pb-1 pt-2.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
            {t("handoff.section")}
          </p>
          <ExportItem
            icon={<Package className="size-4" />}
            title={t("handoff.package.title")}
            sub={t("handoff.package.sub")}
            busy={busy === "package"}
            busyLabel={t("preparing")}
            disabled={!!busy && busy !== "package"}
            onPick={() => pick("package", packageUrl, `${jobId}-editor-package.zip`, false)}
          />
          <ExportItem
            icon={<Clapperboard className="size-4" />}
            title={t("handoff.premiere.title")}
            sub={t("handoff.premiere.sub")}
            busy={busy === "edl"}
            busyLabel={t("preparing")}
            disabled={!!busy && busy !== "edl"}
            onPick={() => pick("edl", edlUrl, `${jobId}.edl`, false)}
          />
          <ExportItem
            icon={<Clapperboard className="size-4" />}
            title={t("handoff.fcp.title")}
            sub={t("handoff.fcp.sub")}
            busy={busy === "fcpxml"}
            busyLabel={t("preparing")}
            disabled={!!busy && busy !== "fcpxml"}
            onPick={() => pick("fcpxml", fcpxmlUrl, `${jobId}.fcpxml`, false)}
          />
        </div>
      )}
      {/* Render-required gate: shown when the user downloads after framing/crop/aspect/trim/motion edits. */}
      <ConfirmRenderModal
        open={confirmOpen}
        onRerender={handleRerender}
        onDownloadAnyway={handleDownloadAnyway}
        onClose={() => {
          setConfirmOpen(false);
          pendingRef.current = null;
        }}
      />
    </div>
  );
}

function ExportItem({
  icon,
  title,
  sub,
  busy = false,
  busyLabel,
  disabled,
  onPick,
}: {
  icon: ReactNode;
  title: string;
  sub: string;
  busy?: boolean;
  /** Required: the localized "preparing" copy shown while this item renders on demand. */
  busyLabel: string;
  disabled?: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled || busy}
      aria-busy={busy}
      onClick={onPick}
      className={`flex w-full items-start gap-2.5 px-3 py-2.5 text-left transition ${
        disabled ? "pointer-events-none opacity-40" : "hover:bg-surface-2"
      }`}
    >
      <span className="mt-0.5 text-muted">
        {busy ? <Loader2 className="size-4 animate-spin text-accent" /> : icon}
      </span>
      <span className="flex flex-col">
        <span className="text-sm font-semibold text-ink">{title}</span>
        <span className="text-xs text-muted">{busy ? busyLabel : sub}</span>
      </span>
    </button>
  );
}
