"use client";

import dynamic from "next/dynamic";

/*
  Public entry point — kept at this path/name so every call site (Hero, WhyQuip,
  Comparison, ClipCard) needs no changes.

  The actual gauge implementation (and the `motion/react` runtime its count-up needs)
  lives in ConfidenceGaugeMotion.tsx and is pulled in lazily via next/dynamic, so that
  runtime ships as its own code-split chunk instead of bloating the bundle every
  section rendering a gauge has to load up front. `ssr: false` is deliberately NOT
  used: the gauge's value is real page content (the visible number + the a11y sr-only
  reading), so it must still come through in the server-rendered HTML for SEO/no-JS —
  only the animation's JS chunk is deferred, not the markup. (Next.js still renders
  this normally on the server with the default `ssr: true`; see
  node_modules/next/dist/docs/01-app/02-guides/lazy-loading.md.)
*/
export const ConfidenceGauge = dynamic(() =>
  import("./ConfidenceGaugeMotion").then((mod) => mod.ConfidenceGauge)
);
