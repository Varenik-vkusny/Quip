# Integration Guide — porting the Quip landing page into the production app

Audience: a developer or coding agent integrating this landing page into the **production Quip app**
(Next.js 16, App Router, Tailwind v4, tokens via `@theme`, fonts via `next/font/google`).

This guide is ordered. Do the steps top to bottom. Each step says exactly what to copy, what to
reconcile, and how to verify. A ready-to-paste **agent prompt** is at the bottom.

---

## 0. What you are integrating

A single marketing page composed of 12 sections, a content layer, a small set of components, and
media assets. It has **no backend, no data fetching, no env vars**. Everything is static. The only
"dynamic" concept is an **auth-aware** flag that swaps CTA labels/links for logged-in visitors
(Step 6).

It is intentionally low-coupling: it brings its own tokens (namespaced to Quip's palette), its own
fonts, and its own components. Nothing here should fight the host app except where you deliberately
reconcile design tokens (Step 2).

---

## 1. Compatibility (Next 15.5 → Next 16)

Everything used here is App-Router-native and unchanged on Next 16:

- Server Components by default; motion/interactive pieces are isolated `"use client"` leaves.
- `next/font/google` for fonts.
- `export const metadata` / `export const viewport` in `layout.tsx`.
- Tailwind v4 `@theme` tokens in a global CSS file imported by the root layout.

Things to check on 16:
- **React 19** is required (this uses it). The prod app on Next 16 is already on React 19.
- If the prod app uses `src/app`, drop the files under `src/app` instead of `app` (adjust the `@/*`
  path alias accordingly — see `tsconfig.json`).
- No use of any 15-only or 16-removed API. No `next/legacy/*`. No middleware.

## 2. Dependencies

Add (if not already present):

```bash
npm i motion @phosphor-icons/react
```

- `motion` — the Motion library (imported as `motion/react`). The reveal animations, the confidence
  count-up, the nav scroll state, the FAQ accordion, and the mobile menu use it.
- `@phosphor-icons/react` — the only icon set. Imported from `@phosphor-icons/react/dist/ssr` so the
  glyphs render in Server Components.

Tailwind v4 + `@tailwindcss/postcss` are assumed already in the host app. **`gsap` is NOT used** — if
you saw it referenced anywhere, ignore it; this package does not depend on it.

## 3. Fonts (Onest + IBM Plex Mono)

The whole type system is two Google fonts wired as CSS variables. They are declared in
`app/layout.tsx`:

```ts
import { Onest, IBM_Plex_Mono } from "next/font/google";
const onest    = Onest({ subsets: ["latin"], variable: "--font-onest",     weight: ["400","500","600","700","800"], display: "swap" });
const plexMono = IBM_Plex_Mono({ subsets: ["latin"], variable: "--font-plex-mono", weight: ["400","500","600"], display: "swap" });
// <html className={`${onest.variable} ${plexMono.variable} grain`}>
```

If the host app already loads these fonts, reuse its variables — but the token names
`--font-onest` and `--font-plex-mono` must resolve, because `globals.css` maps them to
`--font-sans` / `--font-display` / `--font-mono`. Either keep these exact variable names or update the
three `--font-*` lines in `globals.css`.

## 4. Design tokens (the important reconcile)

`app/globals.css` opens with `@theme { ... }` defining the full Quip palette, type scale, radii, and
motion curves. **These are the same tokens shipped in the brief's `brand/quip-design-tokens.css`**,
plus a few additions this page introduced.

Three possible situations:

- **Host app has no Quip tokens yet** → copy the whole `@theme` block as-is.
- **Host app already has the Quip tokens** (likely, since prod ships `quip-design-tokens.css`) → do
  **not** duplicate. Keep the host's `@theme`, and only append the *additions* below.
- **Host token values differ** → the host wins for brand colors; just make sure every token name this
  page references exists.

### Additions this page introduced (append these if missing)

```css
/* in @theme */
--ease-out: cubic-bezier(0.23, 1, 0.32, 1);   /* strong ease-out for entrances */

/* utilities, below @theme */
.container-page { width:100%; max-width: var(--container-page); margin-inline:auto; padding-inline:1.5rem; }
@media (min-width:768px){ .container-page{ padding-inline:2rem; } }

.graticule { /* faint instrument grid behind the hero */ }
.grain::after { /* fixed, pointer-events-none film grain overlay (perf-safe) */ }
.lift { /* card hover-lift, transform/opacity only, gated to (hover:hover) */ }
```

The full, authoritative versions live in `app/globals.css` — copy that file's utilities verbatim
rather than hand-typing them. Token names the components rely on: `--color-bg`, `--color-surface(-2/-3)`,
`--color-ink`, `--color-muted`, `--color-faint`, `--color-line`, `--color-line-strong`,
`--color-accent`, `--color-accent-2`, `--color-accent-tint`, `--color-accent-line`,
`--color-hook/peak/thought/quote`, `--color-ok/warn/bad`, `--radius-*`, `--ease-snappy`, `--ease-out`,
`--container-page`, and the `--text-*` heading scale.

> `sr-only` is a built-in Tailwind v4 utility (used by the confidence gauge for accessible scores). No
> action needed.

## 5. Files to copy

| From (this package) | To (host app)                         | Notes |
|---------------------|---------------------------------------|-------|
| `app/components/*`  | `app/(marketing)/components/` or shared | 6 files, self-contained |
| `app/sections/*`    | `app/(marketing)/sections/`           | 12 sections, one per file |
| `lib/content.ts`    | `lib/landing-content.ts` (or keep name) | the copy/data layer |
| `app/globals.css`   | merge into host global CSS            | see Step 4 (reconcile tokens) |
| `app/layout.tsx`    | merge fonts + metadata into host layout | do **not** blindly overwrite the host layout |
| `app/page.tsx`      | the marketing route's `page.tsx`      | see Step 6 + 7 |
| `public/clips/*`, `public/media/*`, `public/brand/*` | host `public/` | see Step 8 |

Keep the relative import structure (`../components/...`, `@/lib/content`) consistent after moving. The
`@/*` alias points at the app root in `tsconfig.json`; match the host's alias.

## 6. Mounting the page + auth-aware CTAs

`app/page.tsx` assembles the sections and owns the single integration seam:

```tsx
// app/page.tsx
const AUTHED = false; // ← the only thing to wire to real auth
export default function Page() {
  return (
    <>
      <Nav authed={AUTHED} />
      <main>
        <Hero authed={AUTHED} /> ... <FinalCta authed={AUTHED} />
      </main>
      <Footer />
    </>
  );
}
```

`authed` flows into `Nav`, `Hero`, and `FinalCta`. When `true`, CTAs become "Open the app / Dashboard"
→ `/dashboard`; when `false`, they are "Try it free / Paste a video link" → `/signup`.

To wire real auth, make the page (or a thin wrapper) a Server Component that reads the session:

```tsx
// example — adapt to the host's auth (Supabase, NextAuth, custom cookie, etc.)
import { getSession } from "@/lib/auth";
export default async function Page() {
  const session = await getSession();
  const authed = Boolean(session?.user);
  return (<><Nav authed={authed} /> ... </>);
}
```

That is the whole auth integration. No other component reads auth.

## 7. CTA routing

All routes live in one place: `ROUTES` in `lib/content.ts`.

```ts
export const ROUTES = {
  signup: "/signup", app: "/dashboard", login: "/login", terms: "/terms", privacy: "/privacy",
};
```

Point these at the real app routes. Every CTA, nav link, and footer link derives from here or from the
anchor links (`#how-it-works`, `#why`, `#pricing`, `#faq`) — there is no hard-coded href scattered in
the components.

## 8. Assets

Copy `public/clips`, `public/media`, and `public/brand` into the host `public/`. Sizes are already
web-optimized (see `docs/ASSETS.md`):

- `media/demo.mp4` ~2.3 MB (the pipeline demo), `clips/clip-1.mp4` ~2.0 MB, `clips/clip-2.mp4` ~1.7 MB.
- Videos are muted, `playsInline`, `loop`, poster-framed, and **play only while in view**
  (`InlineClip.tsx` uses an IntersectionObserver). Reduced-motion holds on the poster.

> **Deploy gotcha (Vercel): root-anchor any `*.mp4` ignore pattern.** An unanchored `*.mp4` line in
> `.vercelignore` matches recursively and will silently drop these clips from the build (the page then
> renders posters with broken video). Use `/*.mp4` (root only) or list paths explicitly, and confirm
> the three mp4s return HTTP 200 on the deployed URL before calling it done.

If the host already serves the brand icons, you can skip `public/brand` and the `app/*.png` /
`favicon.ico` — but make sure `app/icon.png`, `app/apple-icon.png`, `app/favicon.ico` exist somewhere
Next can pick up for metadata, or remove the `icons` block from `layout.tsx`.

## 9. SEO / metadata

`app/layout.tsx` exports `metadata` (title template, description, OpenGraph, Twitter card, icons) and
`viewport` (`themeColor #0c0b09`, `colorScheme: dark`). Reconcile with the host:

- Set `metadataBase` to the real origin (`https://quip.ink`).
- The OG/Twitter image is `/media/demo-poster.jpg`. Swap for a dedicated 1200×630 OG card if you have one.
- If the host root layout already defines global metadata, merge — keep the host's `metadataBase` and
  layer this page's title/description/OG via the route segment's own `metadata` export.

## 10. Verify (don't skip)

```bash
npm run build         # must be clean: type-check + static prerender
npm run start         # then run Lighthouse (desktop) on the route
```

Checklist:
- [ ] Build is clean, no type errors.
- [ ] Lighthouse: Accessibility / Best Practices / SEO ≥ the baselines (was 100/100/100).
- [ ] The three mp4s load (network tab 200) and autoplay-in-view works; posters show first.
- [ ] CTAs route to the real `/signup` and `/dashboard`; auth-aware swap works when logged in.
- [ ] `prefers-reduced-motion`: videos hold on poster, count-ups show final values, no movement.
- [ ] Mobile (≤640px): hamburger menu opens; all sections single-column; hero CTA visible without scroll.
- [ ] Grep the rendered HTML for the em-dash character — there must be **zero** (house rule).

## 11. Decisions & gotchas to respect

- **Coral is a scarce signal.** One coral mark per viewport in marketing chrome. The four functional
  badge hues (HOOK coral / PEAK purple / THOUGHT green / QUOTE blue) and the coral Download button are
  allowed **only inside framed product surfaces** (the clip cards, the dashboard), never in chrome.
  See `docs/DESIGN.md`.
- **Illustrative vs real content.** The video clips are real Quip output and their on-card hooks match
  the clips' real burned-in hook plates. The poster-only cards and some scores are *illustrative
  examples* (in the brief's spirit). See `docs/CONTENT.md` → *Honesty notes* before you change numbers.
- **No fabricated proof.** No fake testimonials, user counts, urgency, or scarcity anywhere. Keep it
  that way.
- **Em-dash ban** is enforced site-wide (headlines, body, alt text, meta). Use hyphens/colons/periods.

---

## Ready-to-paste agent prompt

> You are integrating a self-contained Next.js App Router landing page (in `quip-landing-delivery/`)
> into our production app (Next.js 16, App Router, Tailwind v4 with `@theme` tokens, fonts via
> `next/font/google`, auth via `<our auth lib>`).
>
> Follow `quip-landing-delivery/docs/INTEGRATION.md` in order. Specifically:
> 1. Add deps `motion` and `@phosphor-icons/react`. Do not add gsap.
> 2. Reconcile `@theme` tokens: we already ship the Quip palette in `brand/quip-design-tokens.css` — do
>    NOT duplicate it; only append the additions listed in Step 4 (`--ease-out`, and the `.container-page`,
>    `.graticule`, `.grain`, `.lift` utilities) and verify every token name the components use resolves.
> 3. Wire fonts: ensure `--font-onest` and `--font-plex-mono` resolve (reuse ours from `layout.tsx` or
>    map to our existing font variables).
> 4. Copy `app/components/*`, `app/sections/*`, and `lib/content.ts` into our marketing route. Keep imports working.
> 5. Mount the page at `<our marketing route>`; make it a Server Component that reads our session and
>    passes `authed` to `Nav`, `Hero`, `FinalCta` (Step 6).
> 6. Point `ROUTES` in `lib/content.ts` at our real `/signup`, `/dashboard`, `/login`, `/terms`, `/privacy`.
> 7. Copy `public/clips`, `public/media`, `public/brand`. Ensure our `.vercelignore` does NOT exclude
>    `*.mp4` recursively (root-anchor it). Confirm the three mp4s 200 on deploy.
> 8. Merge `layout.tsx` metadata into ours; set `metadataBase` to `https://quip.ink`.
> 9. Run `npm run build` (must be clean) and Lighthouse; confirm zero em-dashes in the rendered HTML,
>    reduced-motion behavior, and the mobile menu.
>
> Constraints you must not violate: coral stays a scarce signal (one per viewport in chrome; functional
> badge hues only inside product surfaces); no fabricated proof; zero em-dashes; dark mode only. When
> unsure about copy or numbers, consult `docs/CONTENT.md` and do not invent metrics.
