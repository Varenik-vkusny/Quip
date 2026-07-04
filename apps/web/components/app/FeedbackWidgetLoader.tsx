"use client";

import dynamic from "next/dynamic";

// FeedbackWidget is mounted site-wide from the root layout (app/layout.tsx) — including
// every SEO-critical marketing/legal page (/terms, /privacy, /pricing, /use-case/*) that
// otherwise ships zero Supabase code. But FeedbackWidget statically imports
// `createSupabaseBrowserClient` (@supabase/ssr → @supabase/supabase-js), so without this
// split every one of those routes' first-load JS pays for the Supabase client just to
// render a floating "Feedback" button that 99% of visits never touch.
//
// `next/dynamic(..., { ssr: false })` isn't allowed directly inside a Server Component
// (app/layout.tsx has no "use client"), so this tiny client wrapper does the dynamic
// import and the layout renders the wrapper instead. Safe to skip SSR: the widget is a
// `position: fixed` overlay with no layout footprint and no SEO content, so deferring its
// chunk to after hydration is invisible other than the FAB appearing a beat later.
const FeedbackWidget = dynamic(
  () => import("./FeedbackWidget").then((m) => m.FeedbackWidget),
  { ssr: false },
);

export function FeedbackWidgetLoader() {
  return <FeedbackWidget />;
}
