import { AppHeader } from "@/components/app/AppHeader";
import { Skeleton } from "@/components/ui/Skeleton";

// Instant app-shell skeleton streamed while the (app) layout's server-side
// `getUser()` auth check is in flight (no loading.tsx previously → blank
// document on a slow/cold auth round-trip). Pure perceived-perf: same shell
// (`min-h-dvh` + real <AppHeader/>) and the same `max-w-[1200px]` main
// container + `Split` "main-rail" proportions as the dashboard's idle state,
// so swapping in the real page causes no layout shift. Server component (no
// "use client") — AppHeader renders standalone (its own client-side auth
// fetch is independent of this gate) and Skeleton has no client directive.
export default function AppLoading() {
  return (
    <div className="min-h-dvh">
      <AppHeader />
      <main className="mx-auto max-w-[1200px] px-5 py-10 sm:px-8 sm:py-12" aria-busy="true">
        <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_340px] lg:gap-12">
          {/* left: mirrors the idle "new project" intake column */}
          <section>
            <Skeleton className="h-3 w-24" />
            <Skeleton className="mt-3 h-9 w-72 max-w-full sm:h-11 sm:w-96" />
            <Skeleton className="mt-3 h-4 w-full max-w-md" />
            <Skeleton className="mt-2 h-4 w-3/4 max-w-md" />
            <div className="mt-8 space-y-3">
              <Skeleton className="h-48 w-full rounded-lg" />
              <Skeleton className="h-11 w-full max-w-[220px] rounded-md" />
            </div>
          </section>

          {/* right rail: usage meter + recent projects shapes */}
          <aside className="space-y-5">
            <section className="rounded-lg border border-line bg-surface p-5">
              <div className="mb-4 flex items-center justify-between">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-3 w-10" />
              </div>
              <div className="flex items-center justify-between">
                <Skeleton className="h-3 w-12" />
                <Skeleton className="h-3 w-14" />
              </div>
              <Skeleton className="mt-4 h-10 w-28" />
              <Skeleton className="mt-4 h-0.5 w-full" />
              <Skeleton className="mt-4 h-10 w-full" />
            </section>

            <section className="rounded-lg border border-line bg-surface p-5">
              <Skeleton className="h-3 w-28" />
              <div className="mt-4 space-y-3">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
              </div>
            </section>
          </aside>
        </div>
      </main>
    </div>
  );
}
