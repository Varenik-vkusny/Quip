"use client";

import { useEffect, useState } from "react";
import { getJob } from "@/lib/api";
import type { ClipOut } from "@/lib/types";
import { VideoMapCockpit } from "./VideoMapCockpit";

// Dev harness: mount the cockpit on a REAL local job (video map polled from the worker, clips from
// GET /jobs/{id}, preview from the worker's /media static mount, real extraction). Used by
// /dev/video-map?jobId=<id>. previewUrl points at the static mount so it needs no media ticket.
const WORKER = process.env.NEXT_PUBLIC_WORKER_URL ?? "";

export function RealJobCockpit({ jobId }: { jobId: string }) {
  const [clips, setClips] = useState<ClipOut[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getJob(jobId)
      .then((j) => {
        if (alive) setClips(j.clips ?? []);
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof Error ? e.message : "failed to load job");
      });
    return () => {
      alive = false;
    };
  }, [jobId]);

  if (err) {
    return (
      <p className="text-sm text-bad">
        Couldn’t load job {jobId}: {err}
      </p>
    );
  }

  return (
    <VideoMapCockpit
      jobId={jobId}
      clips={clips}
      previewUrl={WORKER ? `${WORKER}/media/${jobId}/preview.mp4` : undefined}
      plan="pro"
    />
  );
}
