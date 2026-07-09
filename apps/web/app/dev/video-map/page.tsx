import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import { notFound } from "next/navigation";
import { RealJobCockpit } from "@/components/videomap/RealJobCockpit";
import { VideoMapCockpit } from "@/components/videomap/VideoMapCockpit";
import { CARDI_B_FIXTURE } from "@/lib/fixtures/videoMapCardiB";

// Dev-only harness for the Video Map cockpit. Deliberately OUTSIDE the (app) route group so it
// renders without the auth gate (app/(app)/layout.tsx redirects to /login when Supabase is
// configured — and locally that points at a dead project). Same URL: /dev/video-map. We mount the
// NextIntlClientProvider here (the (app) layout normally provides it) because the real ClipGrid we
// render under the Clips tab calls useTranslations.
//   • no `?jobId`      → the hand-authored Cardi B fixture (no reprocessing)
//   • `?jobId=<id>`    → the real results layout (Clips / Video map tabs) on a real local job
// searchParams is a Promise in this Next.js version (see node_modules/next/dist/docs page.js).

export default async function DevVideoMapPage({
  searchParams,
}: {
  searchParams: Promise<{ jobId?: string | string[] }>;
}) {
  if (process.env.NODE_ENV === "production") notFound();

  const sp = await searchParams;
  const jobId = Array.isArray(sp.jobId) ? sp.jobId[0] : sp.jobId;
  const [locale, messages] = await Promise.all([getLocale(), getMessages()]);

  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      <main className="mx-auto max-w-6xl px-4 py-8">
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
    </NextIntlClientProvider>
  );
}
