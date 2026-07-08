import { notFound } from "next/navigation";
import { RealJobCockpit } from "@/components/videomap/RealJobCockpit";
import { VideoMapCockpit } from "@/components/videomap/VideoMapCockpit";
import { CARDI_B_FIXTURE } from "@/lib/fixtures/videoMapCardiB";

// Dev-only harness for the Video Map cockpit. Deliberately OUTSIDE the (app) route group so it
// renders without the auth gate (app/(app)/layout.tsx redirects to /login when Supabase is
// configured — and locally that points at a dead project). Same URL: /dev/video-map.
//   • no `?jobId`      → the hand-authored Cardi B fixture (no reprocessing)
//   • `?jobId=<id>`    → the real poll path (getVideoMap), still no reprocessing
// searchParams is a Promise in this Next.js version (see node_modules/next/dist/docs page.js).

export default async function DevVideoMapPage({
  searchParams,
}: {
  searchParams: Promise<{ jobId?: string | string[] }>;
}) {
  if (process.env.NODE_ENV === "production") notFound();

  const sp = await searchParams;
  const jobId = Array.isArray(sp.jobId) ? sp.jobId[0] : sp.jobId;

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      {jobId ? (
        <RealJobCockpit jobId={jobId} />
      ) : (
        <VideoMapCockpit
          jobId={CARDI_B_FIXTURE.jobId}
          clips={CARDI_B_FIXTURE.clips}
          initialMap={CARDI_B_FIXTURE.map}
          previewUrl={CARDI_B_FIXTURE.previewUrl}
          plan="pro"
          demoMode
        />
      )}
    </main>
  );
}
