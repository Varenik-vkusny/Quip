# Design System — Quip

> Single source of truth for the **production shell** (marketing site, auth, dashboard, account,
> editor chrome). Tokens live in `apps/web/app/globals.css` (`:root` + `[data-theme="dark"]` →
> `@theme inline`). Change values there → whole UI re-skins in BOTH themes.
> Read this before any visual/UI decision. In QA/review, flag anything that deviates.

## Product Context
- **What it is:** Quip turns a long video (podcast / interview / stream) into 3–10 vertical
  clips with burned-in subtitles, a hook on top, and an explanation of **why** each one will land.
- **Who:** solo creators, podcasters, video editors, studios repurposing long content (RU + EN).
- **Wedge (the whole narrative):** not "more clips" (Vizard) — **"fewer clips, but you know WHY
  to post them."** Explainability = hook + why_works + confidence score + clip-type. Plus reframe
  with no flashes and honest pricing (no credit-casino).
- **Memorable thing:** *a precise, honest instrument you trust*, staged like a product you'd pay for.
- **Message:** "Don't just get clips. Know why they're worth posting."

## Direction — «Studio» / Signal Gold (founder-chosen 2026-07-09; supersedes Warm Precision)
- **Decision:** full redesign. Brand accent moved coral → **Signal Gold** (founder picked from
  gold / magenta / evolved-coral options). Coral is now purely the functional `hook` moment color;
  the brand finally has its own lane (the old system's core conflict).
- **Two themes from one token set:**
  - **light «stage»** — marketing default. Warm off-white, white cards, stacked micro-shadows
    (Vercel recipe). Communicates "trustworthy, worth paying for" (ElevenLabs/Stripe model).
  - **dark «studio»** — app/auth/editor default. Warm near-black surface ladder + hairlines
    (Linear recipe). A pro edit bay.
  - Explicit user choice (localStorage `quip-theme`, ThemeToggle) wins everywhere; otherwise the
    surface default applies (pre-paint script in `app/layout.tsx` + `ThemeApplier` on SPA nav).
    **No theme cookie ever in the root layout** — marketing must stay SSG.
- **Mood:** measured, premium, alive. The landing is scroll-choreographed (Lenis + GSAP) with one
  signature element: the **WaveformRiver** — an hour of conversation flowing by while the AI
  ignites moments in gold. Motion is motivated (tells the product story), never decorative loops.
- **Reference:** design-md kit (linear, elevenlabs, resend, framer as the core family).

## Color
Approach: **one scarce brand accent (gold) on chrome only** — logo, primary CTA, focus ring,
active nav. Moment colors are **quarantined inside clip cards/badges** and never touch chrome.
No neon glows, no decorative accent washes (one atmospheric wash in the hero max).

| Token (`--color-*`) | Light «stage» | Dark «studio» | Use |
|---|---|---|---|
| `bg` | `#FAF9F5` | `#0D0C0A` | page canvas (warm) |
| `surface` | `#FFFFFF` | `#15130F` | cards, panels |
| `surface-2` | `#F2F0EA` | `#1C1915` | raised / nested |
| `surface-3` | `#EAE7DF` | `#24201A` | hover / active fill |
| `ink` | `#1C1A17` | `#F4F2EC` | primary text |
| `muted` | `#57534A` | `#A29B8D` | secondary text |
| `faint` | `#6D6759` | `#8B8476` | fine print (AA on bg) |
| `line` / `line-strong` | ink @ 9% / 16% | ink @ 8% / 15% | hairlines |
| `accent` | `#F0A400` | `#FFB224` | **gold fill** (CTA, logo); text on it = `on-accent` |
| `accent-2` | `#DD9500` | `#FFC24D` | hover/pressed fill |
| `accent-ink` | `#8F6400` | `#FFC24D` | gold as TEXT (AA-safe per theme) |
| `on-accent` | `#1C1A17` | `#1C1A17` | text/icon on gold fill |
| `hook` | `#D93B1F` | `#FF5A3D` | moment chip (cards only) |
| `peak` | `#8B3DD9` | `#C06BFF` | moment chip |
| `thought` | `#0E8A63` | `#19BD8B` | moment chip |
| `quote` | `#2E6BE6` | `#4D8DFF` | moment chip |
| `ok` / `warn` / `bad` | `#0E8A63/#B45309/#C42B2B` | `#19BD8B/#F5B32E/#FF6B6B` | semantic; confidence = `ok` |

Each moment color also has a `*-tint` background token. **Never** put moment colors on buttons,
nav, or section chrome.

## Depth
- **Light:** stacked micro-shadows (`shadow-card`, `shadow-card-hover`, `shadow-pop` tokens) —
  2–3 tiny offsets + inset hairline ring. Never one fat blurry drop, never colored glows.
- **Dark:** surface ladder + hairlines; `shadow-pop` (hairline + deep drop) only for floating
  cards (reason card, product windows).
- The app-in-marketing trick: a window framed with `data-theme="dark"` scopes the studio tokens
  to that subtree (see `StudioWindow.tsx`) — the same runtime mechanism as the global switch.

## Type
- **Onest** (display + body; Cyrillic-native) + **IBM Plex Mono** (timecodes, confidence scores,
  prices — always `tnum`). Loaded via `next/font` in `app/layout.tsx`.
- Premium retune (2026-07-09): display weight ceiling **650** (no 700–800 shouting), tight
  tracking −2.6…−3%. Semantic scale tokens: `text-display-2xl/xl/lg`, `text-h2/h3`, `text-lead`.
- Hierarchy comes from weight + color, not raw size. Body ≤ 65ch.
- **Zero em-dashes** anywhere user-facing (brand + anti-slop rule).

## Motion (emil rules)
- Transform/opacity only; strong ease-out `var(--ease-out-strong)`; press feedback `scale(.97)`.
- Landing choreography lives in ONE client island `components/landing/fx/ScrollFx.tsx`
  (Lenis inertia, nav pill morph, hero parallax, count-ups, pinned film-strip pan, manifesto word
  scrub, 3D product-shot entrance) + the `WaveformRiver` canvas. Sections stay server components
  and opt in via data-attributes. Everything degrades to static under `prefers-reduced-motion`
  and no-JS (content never hidden).
- App/editor: no scroll theater; tactile micro-feedback only.

## Anti-slop rules (enforced in review)
- Max ~1 eyebrow per 3 sections; no `//`-prefixed or numbered section labels; no mono-caps trust
  lines; no marquee strips (founder-vetoed as "AI-ish"); no decorative dots; no fake metrics.
- One gold element per fold. No purple AI gradients. Cards only where elevation means something.
- Real product media over illustration: the landing runs on actual clips from `public/clips/`
  + `public/demo/`.

## History
- 2026-06-13 — «Warm Precision» (concept C): warm near-black + scarce coral, dark-only. Superseded.
- 2026-07-09 — «Studio» / Signal Gold: dual themes, gold brand lane, scroll choreography,
  WaveformRiver signature. Branch `feat/premium-redesign`.
