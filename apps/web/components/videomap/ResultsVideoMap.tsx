"use client";

import { useEffect, useState } from "react";
import { getMediaToken, withToken } from "@/lib/api";
import type { ClipOut } from "@/lib/types";
import { VideoMapCockpit } from "./VideoMapCockpit";

// The Video Map on the real results page: the cockpit fed with the job's clips + the token'd source
// preview proxy (same media-ticket path the editor's PreviewPlayer uses) + the owner's plan (drives
// the Extract gate). The preview resolves async; the cockpit shows a calm placeholder until then.
const WORKER = process.env.NEXT_PUBLIC_WORKER_URL ?? "";

export function ResultsVideoMap({
  jobId,
  clips,
  plan,
}: {
  jobId: string;
  clips: ClipOut[];
  plan?: string | null;
}) {
  const [previewUrl, setPreviewUrl] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!WORKER) return;
    let alive = true;
    const base = `${WORKER}/jobs/${jobId}/preview.mp4`;
    getMediaToken(jobId)
      .then((token) => {
        if (alive) setPreviewUrl(withToken(base, token));
      })
      .catch(() => {
        // No ticket (dev/open worker) → the endpoint is pass-through there; use the bare URL.
        if (alive) setPreviewUrl(base);
      });
    return () => {
      alive = false;
    };
  }, [jobId]);

  return <VideoMapCockpit jobId={jobId} clips={clips} previewUrl={previewUrl} plan={plan} />;
}
