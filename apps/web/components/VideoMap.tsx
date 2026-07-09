"use client";

import { VideoMapCockpit } from "@/components/videomap/VideoMapCockpit";
import type { ClipOut } from "@/lib/types";

// Thin shim: the Video Map is now the cockpit (source preview + timeline + outline). This keeps the
// `{ jobId, clips? }` mount at app/(app)/dashboard/page.tsx stable. Real implementation lives in
// components/videomap/VideoMapCockpit.tsx.
export function VideoMap({ jobId, clips = [] }: { jobId: string; clips?: ClipOut[] }) {
  return <VideoMapCockpit jobId={jobId} clips={clips} />;
}
