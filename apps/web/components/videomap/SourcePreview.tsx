"use client";

import { useEffect, useRef } from "react";

// Inline source-video preview for the cockpit. Drives a per-frame playhead via
// requestVideoFrameCallback (falls back to `timeupdate` only when rVFC is unsupported) and exposes
// an imperative seek through `seekRef` so the outline/timeline can jump the video without a React
// re-render round-trip (same reason PreviewPlayer uses rVFC — the 4Hz `timeupdate` event lags).

type RVFCVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (
    cb: (now: number, meta: { mediaTime: number }) => void,
  ) => number;
  cancelVideoFrameCallback?: (id: number) => void;
};

export function SourcePreview({
  previewUrl,
  onTime,
  seekRef,
}: {
  previewUrl: string;
  onTime: (sec: number) => void;
  seekRef: React.MutableRefObject<((sec: number) => void) | null>;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  // Keep the latest onTime in a ref so the frame loop never re-subscribes (a new closure each render
  // would tear down + re-register the rVFC loop every keystroke of parent state).
  const onTimeRef = useRef(onTime);
  useEffect(() => {
    onTimeRef.current = onTime;
  }, [onTime]);

  // Publish the imperative seek: set currentTime + play. Cleared on unmount so a stale closure can't
  // seek a gone element.
  useEffect(() => {
    const ref = seekRef;
    ref.current = (s: number) => {
      const v = videoRef.current;
      if (!v) return;
      try {
        v.currentTime = Math.max(0, s);
      } catch {
        /* seek before loadedmetadata can throw — harmless, next play() retries */
      }
      void v.play().catch(() => {
        /* autoplay/gesture policies — the frame still seeks, that's what we need */
      });
    };
    return () => {
      ref.current = null;
    };
  }, [seekRef]);

  // Per-frame playhead. rVFC fires on every presented frame (playing OR seeking) → the playhead
  // tracks the actual pixels; `timeupdate` (~4Hz) is the fallback only.
  useEffect(() => {
    const video = videoRef.current as RVFCVideo | null;
    if (!video) return;
    let stopped = false;
    let vfcId = 0;
    const push = (t: number) => {
      if (!stopped) onTimeRef.current(t);
    };
    const rvfc = video.requestVideoFrameCallback;
    if (typeof rvfc === "function") {
      const onFrame = (_now: number, meta: { mediaTime: number }) => {
        if (stopped) return;
        push(meta.mediaTime);
        vfcId = rvfc.call(video, onFrame);
      };
      vfcId = rvfc.call(video, onFrame);
      return () => {
        stopped = true;
        const cancel = video.cancelVideoFrameCallback;
        if (vfcId && typeof cancel === "function") cancel.call(video, vfcId);
      };
    }
    const onTimeUpdate = () => push(video.currentTime);
    video.addEventListener("timeupdate", onTimeUpdate);
    return () => {
      stopped = true;
      video.removeEventListener("timeupdate", onTimeUpdate);
    };
  }, []);

  return (
    <div className="overflow-hidden rounded-lg border border-line bg-black">
      <video
        ref={videoRef}
        src={previewUrl}
        playsInline
        preload="metadata"
        controls
        className="block aspect-video w-full"
      />
    </div>
  );
}
