"use client";

import dynamic from "next/dynamic";

/*
  Public entry point — kept at this path/name so every below-the-fold section that
  does `import { Reveal } from "../components/Reveal"` needs no changes.

  The actual fade-up implementation (and the `motion/react` runtime it needs) lives in
  RevealMotion.tsx and is pulled in lazily via next/dynamic, so that runtime ships as
  its own code-split chunk instead of bloating the bundle every section using Reveal
  has to load up front. `ssr: false` is deliberately NOT used: `children` here is real
  page content (headings, copy, cards) that must still come through in the server-
  rendered HTML for SEO/no-JS — only the animation's JS chunk is deferred, not the
  markup. (Next.js still renders this normally on the server with the default
  `ssr: true`; see node_modules/next/dist/docs/01-app/02-guides/lazy-loading.md.)
*/
export const Reveal = dynamic(() => import("./RevealMotion").then((mod) => mod.Reveal));
