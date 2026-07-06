# Quip Landing Page

A complete, production-ready marketing landing page for **Quip** (quip.ink), the explainable-AI clip
tool. Built as a self-contained Next.js App Router app so it runs on its own **and** ports cleanly into
the production site.

Design direction: **"The Readout"** — the page is built from Quip's *real product surfaces* (a live
dashboard mid-run, real captioned 9:16 output clips, the reasoning panel) on a warm near-black canvas
with one scarce coral signal per view. It proves the product by *running it*, not describing it.

---

## What's in the box

```
quip-landing-delivery/
├─ README.md                    ← you are here (overview + quick start)
├─ docs/
│  ├─ INTEGRATION.md            ← STEP-BY-STEP port into the production app  (start here to integrate)
│  ├─ DESIGN.md                 ← design system: tokens, type, motion, layout map, decisions
│  ├─ CONTENT.md                ← the copy layer, how to edit it, honesty notes, exact pricing
│  └─ ASSETS.md                 ← every asset + the ffmpeg pipeline used to make them
│
├─ app/
│  ├─ layout.tsx                ← fonts (next/font), <head> metadata / OG / Twitter, theme color
│  ├─ page.tsx                  ← section assembly + the AUTHED flag (auth-aware slot)
│  ├─ globals.css               ← @theme design tokens + utilities (graticule, grain, lift, easing)
│  ├─ icon.png / apple-icon.png / favicon.ico   ← Next.js auto-served metadata icons
│  ├─ components/               ← reusable primitives (see DESIGN.md → Component inventory)
│  └─ sections/                 ← the 12 page sections, one file each
│
├─ lib/
│  └─ content.ts                ← SINGLE SOURCE OF TRUTH for all copy + clip/pricing/FAQ data
│
├─ public/
│  ├─ brand/                    ← logo mark + favicon (also mirrored in app/ for metadata)
│  ├─ media/                    ← demo.mp4 (the pipeline demo) + its poster
│  └─ clips/                    ← real captioned 9:16 output clips + still frames
│
├─ package.json, tsconfig.json, next.config.mjs, postcss.config.mjs
```

## Quick start (run it standalone)

```bash
cd quip-landing-delivery
npm install
npm run dev          # http://localhost:3000
# or a production build:
npm run build && npm run start
```

Node 18.18+ (built and verified on Node 22). No env vars required to run.

## Tech stack

| Concern    | Choice                                                              |
|------------|--------------------------------------------------------------------|
| Framework  | Next.js 15.5, App Router, React Server Components by default        |
| Styling    | Tailwind CSS v4 (tokens via `@theme` in `app/globals.css`)          |
| Fonts      | `next/font/google`: **Onest** (display+body) + **IBM Plex Mono** (numerals) |
| Motion     | **Motion** (`motion/react`) — isolated in `"use client"` leaf components |
| Icons      | `@phosphor-icons/react` (SSR import path) — the only icon set used   |

> The production site is Next.js 16. This package is Next.js 15.5; the App Router APIs used here
> (`next/font`, `@theme`, RSC, `metadata`/`viewport` exports) are identical on 16. See
> `docs/INTEGRATION.md` → *Compatibility*.

## Status (verified)

- Production build: **clean** (type-check passes, fully static prerender)
- Lighthouse (desktop): **Accessibility 100 · Best Practices 100 · SEO 100**
- Core Web Vitals: **LCP ~0.26s · CLS 0.00**
- **Zero em-dashes** anywhere (brand + house style), reduced-motion honored, mobile menu, keyboard-navigable

## Brand constants (do not break)

- Name **Quip**, domain **quip.ink**
- Coral **`#FF5A3D`** — used *scarcely* (one signal per view)
- Warm near-black canvas **`#0C0B09`**, warm off-white ink **`#F2EFE9`**
- **Onest** + **IBM Plex Mono** (mono for all numerals: scores, timecodes, prices, counts)
- Dark mode only; hairlines over shadows

---

**To integrate into the main app, open `docs/INTEGRATION.md`.** It is written so you can hand it to a
coding agent and follow it top to bottom.
