"use client";

import { useEffect, useState } from "react";
import { ClipGrid } from "@/components/ClipGrid";
import { getJob } from "@/lib/api";
import type { Job } from "@/lib/types";
import { ResultTabs } from "./ResultTabs";
import { VideoMapCockpit } from "./VideoMapCockpit";

// Dev harness (/dev/video-map?jobId=<id>): renders the REAL results-page layout on a real local job
// — the Clips / Video map TABS, with the actual ClipGrid and the cockpit (map polled from the
// worker, preview from the worker's /media static mount, real extraction). Mirrors what ships on the
// dashboard so the tabbed layout can be dogfought locally with no reprocessing.
const WORKER = process.env.NEXT_PUBLIC_WORKER_URL ?? "";

export function RealJobCockpit({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getJob(jobId)
      .then((j) => {
        if (alive) setJob(j);
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
  if (!job) {
    return <p className="text-sm text-muted">Loading {jobId}…</p>;
  }

  return (
    <ResultTabs
      clipCount={job.clips?.length ?? 0}
      clips={<ClipGrid key={job.id} job={job} />}
      map={
        <VideoMapCockpit
          jobId={jobId}
          clips={job.clips ?? []}
          previewUrl={WORKER ? `${WORKER}/media/${jobId}/preview.mp4` : undefined}
          plan="pro"
        />
      }
    />
  );
}
