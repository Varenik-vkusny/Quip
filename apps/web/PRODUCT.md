# Product

## Register

product

## Platform

web

## Users

Solo creators, podcasters, video editors, and studios who sit on hours of long-form
footage — podcasts, interviews, streams — and need to turn it into vertical shorts worth
posting. Their context is repurposing: the raw material already exists, and the job is to
find and finish the moments that will land, without gambling credits or guessing what to
publish. The audience is bilingual **RU + EN**, and both languages are treated as equal
first-class citizens across the funnel, not English-first with a translation bolted on.

The primary surface these people live in is the **product app** — upload, dashboard,
editor, account. The marketing site is a distinct, deliberately crafted acquisition
surface (handled per task as a brand surface), but the default home base for ongoing
design work is the tool itself.

## Product Purpose

Quip turns one long video into 3–10 vertical clips, each with burned-in subtitles, a hook
on top, and — the thing that sets it apart — an explanation of **why** that clip will land
(hook + why_works + a confidence score + a clip-type). The pipeline also reframes to 9:16
without the mode-switch flashes that plague competitors, and prices honestly rather than
running a credit-casino.

Success is **free → paid conversion**: the product has to prove its value fast enough that
hitting the free single-video limit reads as a reason to upgrade, not a reason to leave.
Every design decision on the core funnel — first upload, processing wait, first result,
the moment the limit bites, the pricing surface — should compound toward that conversion.

## Brand Personality

Precise, honest, premium. The instrument metaphor is the whole voice: Quip is a tool you
**trust** because it shows its reasoning and never inflates its numbers. The tone is
confident without hype — measured and alive, staged like a product you'd pay for, never a
carnival of credits and fake urgency. Copy stays plain and direct; Russian must be genuinely
idiomatic (never machine-translated, never mixed RU/EN in one string), and there are zero
em-dashes anywhere user-facing.

## Anti-references

Not Vizard's "more clips is better" credit-casino — volume without judgment is the exact
thing Quip rejects. And not the generic AI-slop house style: no purple AI gradients, no
gradient text, no marquee logo strips, no decorative dots, no invented metrics, no mono-caps
trust lines, no `//`-prefixed or numbered section labels, no eyebrow above every section, no
neon glows, and no shouting display weights. If a visitor could look at a screen and say "AI
made that," it has failed.

## Design Principles

**Explainability over volume.** Every clip earns its place with a hook, a why-it-works, and
a confidence score — the interface should always answer "why this one," never just hand over
a count. This is the product's reason to exist; the UI must make it legible.

**Honest instrument.** Trust is the product. No credit-casino mechanics, no fake metrics, no
dark patterns around the paywall — transparent pricing and precise, truthful feedback are
design constraints, not marketing copy.

**Show, don't tell.** Prove the product with the product. Real clips and real reasoning carry
more conviction than illustration or claims; let the artifact sell itself.

**Motion with a reason.** Every animation should carry product meaning (a moment igniting, a
waveform flowing) rather than decorate. Motion that doesn't tell a story gets cut, and all of
it degrades gracefully to static.

**Bilingual as a first-class citizen.** RU and EN are equals. Layouts, copy, and typography
must hold up idiomatically in both; nothing is designed English-first and translated after.

## Accessibility & Inclusion

Target **WCAG 2.1 AAA where feasible** without fighting the premium aesthetic, with AA as the
hard floor that never slips: body text at least 4.5:1 (pushing 7:1 where it doesn't break the
look), large text at least 3:1, placeholder text held to the same body bar. Fully keyboard
navigable with visible focus (the scarce gold focus ring), semantic HTML, and honest numeric
formatting (tabular figures for timecodes, confidence scores, and prices).

Reduced motion is non-negotiable: the entire landing choreography degrades to static under
`prefers-reduced-motion`, and content is never gated behind a JS- or animation-triggered
reveal — it must be present and readable with no motion and no JS. Both themes (light «stage»,
dark «studio») must independently pass contrast. Bilingual support extends to a dynamic
`<html lang>` so assistive tech announces RU and EN correctly.
