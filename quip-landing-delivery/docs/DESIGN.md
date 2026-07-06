# Design System — Quip landing ("The Readout")

This documents the visual language so the direction survives future edits. The page was chosen and
tuned against a 3-judge panel and two craft skills (anti-slop taste pass + Emil Kowalski's
design-engineering motion principles).

---

## 1. The idea

**The page is a readout of a precise instrument.** Quip's whole pitch is "not more clips — fewer
clips, but you know *why* to post them," so the design *is* that argument: it shows the actual product
mid-run (a dashboard, real clips, a confidence gauge) instead of marketing mockups, and it treats the
0–100 confidence score as the hero motif. The interface chrome stays monochrome and hairline-thin so
the real footage and the one coral signal carry the page.

Tone: measured, precise, a little dry. Premium and trustworthy (Linear / Vercel / Warp restraint),
with coral as the single human spark.

## 2. Brand constants (hard)

| Token        | Value      | Use |
|--------------|------------|-----|
| Coral        | `#FF5A3D`  | **scarce** — one signal per viewport: primary CTA, the active confidence score, the hero word-chip, focus rings |
| Canvas       | `#0C0B09`  | warm near-black, the page background = "the whitespace" |
| Ink          | `#F2EFE9`  | warm off-white text |
| Display+body | **Onest**  | weights 400–800, tight negative tracking on headlines |
| Numerals     | **IBM Plex Mono** | *every* number: scores, timecodes, prices, counts (tabular figures) |

Dark mode only. **Hairlines over shadows**: elevation comes from a 4-rung warm surface ladder plus 1px
borders, not drop shadows.

## 3. Tokens (in `app/globals.css` `@theme`)

- **Surface ladder:** `--color-bg #0C0B09` → `--color-surface #15120E` → `--color-surface-2 #1C1813` →
  `--color-surface-3 #241F18`. Going up a rung = "lifted/active".
- **Text ladder:** `--color-ink #F2EFE9` → `--color-muted #9A8F80` → `--color-faint #8B8276` (AA-safe
  for fine print on the canvas).
- **Hairlines:** `--color-line` (8% warm white) and `--color-line-strong` (14%).
- **Accent:** `--color-accent #FF5A3D`, press `--color-accent-2 #E0431F`, `--color-accent-tint`
  (12%), `--color-accent-line` (30%).
- **Functional clip-type hues (product surfaces only):** `--color-hook #FF5A3D`, `--color-peak
  #C06BFF`, `--color-thought #19BD8B`, `--color-quote #4D8DFF`.
- **Semantic:** `--color-ok`, `--color-warn`, `--color-bad #FF6B6B` (the last drives the "scored too
  low" red bars in the comparison).
- **Radii:** buttons 10px, cards 14–18px, tiny badges full-pill. (Buttons are deliberately *not* pills
  — crisper, more instrument-like, per the Linear reference.)
- **Type scale:** `--text-display-2xl/xl/lg`, `--text-h2/h3`, `--text-lead`, `--text-eyebrow`, each
  carrying its own line-height / tracking / weight.
- **Motion:** `--ease-snappy cubic-bezier(.2,.7,.2,1)`, `--ease-out cubic-bezier(.23,1,.32,1)`.

## 4. The coral-scarcity rule (most important)

Coral is metered to **roughly one mark per viewport** in the page's own chrome. This is what keeps it
a *signal* (a verdict, the one action) instead of decoration.

- Allowed coral in chrome: the primary CTA, the active confidence numeral, the hero word-chip
  (`why`), focus rings, the section "kicker" tick, the recommended-plan treatment, the comparison
  "winner" accents.
- The four functional badge hues and the coral Download button are **product reality** — allowed only
  *inside* framed product surfaces (clip cards, the dashboard panel), never leaking into page chrome.
- Example of the rule resolving a conflict: in card grids only the **active** card's score ignites
  coral; resting cards keep their score in ink (`ConfidenceGauge accent={false}`).

## 5. Type system

- Onest carries every headline and paragraph; never set "loud" — emphasis is weight + negative
  tracking, not raw size. The one display flourish is the coral word-chip on a single hero word.
- IBM Plex Mono carries **every numeral and technical label** with `tnum` tabular figures, slightly
  tightened, so scores/prices/timecodes lock into columns (the "instrument" feel). The `.num` utility
  applies this.
- **Section kicker** (the small label above a few section headings): a short coral tick + a brighter
  mono label (`components/primitives.tsx → Eyebrow`). Rationed to a few sections, not every one.

## 6. Motion (Emil principles)

- **Entrances:** `--ease-out` (strong), ~0.5–0.6s, `transform`+`opacity` only, staggered ~60ms across
  grid items (`components/Reveal.tsx`, Motion `whileInView`, `once`).
- **Press feedback:** buttons `active:scale-[0.97]` + slight hover lift (`components/CTA.tsx`).
- **Hover lift:** cards use `.lift` (translateY(-3px) + warm bloom), gated behind
  `@media (hover:hover) and (pointer:fine)` so touch devices don't false-trigger.
- **Signature beat — the confidence gauge** (`components/Confidence.tsx`): on first scroll-in the score
  counts up from 0 and **ignites coral only at lock** ("the needle settles"); runs once; reduced-motion
  shows the final coral value instantly. The real value is always in the a11y tree (`sr-only`), so
  screen readers never read "0".
- **No `window.addEventListener('scroll')`** anywhere — nav scroll state uses Motion's `useScroll`.
- Mood is **crisp, not bouncy** (it's an instrument): no spring overshoot in UI.

## 7. Section layout map (`app/page.tsx` order)

Each section uses a *different* layout family on purpose (no two share one):

1. **Nav** — thin sticky rail, auth-aware, mobile disclosure menu.
2. **Hero** — asymmetric split: left value-prop + CTA, right a live product readout (dashboard chrome +
   real clip + the giant coral confidence gauge). Faint instrument graticule behind.
3. **Demo** — full-bleed dark well playing the real pipeline video; pipeline stage strip; the page's one
   coral atmosphere wash, anchored at the top and decaying fast.
4. **CostAnchor** — tabular 3-cell ledger; the `$0` is the lone coral mark.
5. **HowItWorks** — 3-step numbered rail (not three equal cards).
6. **WhyQuip** — the centerpiece "anatomy of a verdict": one real clip dissected into its four carried
   reasons (hook / why / confidence / type), then an evidence row of real clip cards.
7. **Craft** — 2×2 hairline matrix; two cells carry real visual proof (caption stills + the 4-ratio
   diagram).
8. **Comparison** — kept-clip (coral, full bar) vs rejected clips (red score-bars), then a head-to-head
   table with a coral-checked "winner" Quip column and a dimmed `×` column.
9. **Pricing** — 3 plan cards (Free/Starter/Pro-recommended) + PAYG strip; coral concentrated on the
   recommended Pro card; cards lift on hover.
10. **FAQ** — borderless accordion, hairline dividers, sharp `+` toggle.
11. **FinalCta** — centered resolving chord with a low coral glow; one primary CTA.
12. **Footer** — logo + tagline + link columns + honest-pricing stripe.

## 8. Accessibility

- Semantic landmarks (`header`/`nav`/`main`/`contentinfo`), real heading order (h1 → h2 → h3).
- Visible on-brand focus ring (coral, defined in `globals.css :focus-visible`).
- All media has descriptive `alt`/`aria-label`; the confidence gauge exposes the real score via
  `sr-only`.
- `prefers-reduced-motion` collapses every animation to a static end state; videos hold on poster.
- WCAG AA contrast verified (Lighthouse Accessibility 100). The faint text tier is AA on the canvas;
  the semantic red passes on dark surfaces.

## 9. Component inventory

| File | Role |
|------|------|
| `components/primitives.tsx` | `Container`, `Section`, `Eyebrow` (coral-tick kicker), `WordChip` (hero coral word), `TypeBadge` (functional hue badge) |
| `components/CTA.tsx` | `PrimaryCTA` (coral fill, near-black text) + `GhostCTA` (hairline) with press-scale |
| `components/Reveal.tsx` | scroll-reveal wrapper (Motion `whileInView`, reduced-motion aware) |
| `components/Confidence.tsx` | the count-up confidence gauge; `accent` prop controls coral ignition |
| `components/InlineClip.tsx` | muted, in-view-only autoplay video with poster + reduced-motion hold |
| `components/ClipCard.tsx` | a clip rendered as an instrument readout (clip + verdict column) |

Sections live in `app/sections/*` (one file each, names match the layout map above).
