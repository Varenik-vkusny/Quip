# Content — copy layer, how to edit, and honesty notes

All user-facing copy and data lives in **one file: `lib/content.ts`**. Components render from it; there
is no copy hard-coded in JSX (except a couple of structural words). Edit `content.ts` and the page
updates everywhere.

---

## 1. How the content layer is organized

Exported constants, one per concern:

| Export | Drives |
|--------|--------|
| `NAV`, `ROUTES` | nav links + all CTA destinations |
| `HERO` | hero eyebrow/headline/subhead/CTAs/trust line (headline is split so one word gets the coral chip) |
| `HERO_CLIP`, `CLIPS` | the clip readouts (see *data model* below) |
| `DEMO` | demo section heading + pipeline stage labels |
| `COST_ANCHOR` | the 3-cell price-anchor ledger (the `$0` cell has `accent: true`) |
| `HOW_IT_WORKS` | the 3 steps |
| `WHY` | the "four things every clip carries" payload |
| `CRAFT` | the 4 craft features |
| `COMPARISON`, `CUT_PROOF` | the head-to-head table + the kept/rejected proof |
| `PRICING` | plans, allowances, features, PAYG, footnote |
| `FAQ` | the 6 Q&A items |
| `FINAL_CTA`, `FOOTER` | closing band + footer columns/legal |

### Clip data model

```ts
type Clip = {
  id: string;          // "03 / 23" — shown as the clip index
  src?: string;        // /clips/clip-1.mp4 — if present, the card PLAYS this video; else shows poster
  poster: string;      // still frame
  hook: string;        // the written hook (matches the clip's real burned-in hook plate when src is a real clip)
  type: "hook" | "peak" | "thought" | "quote";  // moment type → badge hue
  confidence: number;  // 0–100 → the gauge
  reason: string;      // one-sentence "why it works"
  timecode: string;    // source range
  caption?: string;    // the burned-in line visible in the still (used for alt text)
};
```

`HERO_CLIP` → the hero readout. `CLIPS[0]` is `HERO_CLIP`; `CLIPS[1]` is the WhyQuip "anatomy" featured
clip; `CLIPS[2]`/`CLIPS[3]` are evidence cards.

---

## 2. Honesty notes (read before changing any number or hook)

The brand's whole identity is "an honest instrument." These rules were followed; keep them.

- **Real footage.** `clip-1.mp4`, `clip-2.mp4`, and `demo.mp4` are real Quip outputs/recordings (real
  captions burned in, including the coral active-word). `demo.mp4` shows the real dashboard; the
  "988 videos · 59,280 min" in it is from the actual recording.
- **The two *video* clips' hooks are accurate.** `HERO_CLIP.hook` ("McConaughey's dad's surprising
  reaction to quitting law school") and `CLIPS[1].hook` ("How Matthew McConaughey earned his dad's
  support") match the **real burned-in hook plates** in those clips, and their `reason`/`type` match
  the clip content. If you swap a video, re-watch it and rewrite the hook/reason/type to match — a card
  whose text contradicts its visible hook plate is the one thing to avoid.
- **Poster-only cards are illustrative.** `CLIPS[2]`/`CLIPS[3]` show stills (no visible hook plate), so
  their hooks/reasons are representative examples. Fine to keep, fine to make real.
- **Some confidence scores are illustrative** (94/91/88/76, and the rejected 38/44/29/51/47 in
  `CUT_PROOF`). They demonstrate the real 0–100 behavior, in the brief's spirit. Swap for real outputs
  if you want exactness; don't inflate them.
- **No fabricated proof anywhere** — no testimonials, user counts, urgency, scarcity. If you add social
  proof later, it must be real.
- **Em-dash ban.** Zero `—`/`–` in any visible string (headlines, body, alt, meta). Use hyphen, colon,
  or two sentences. This is enforced; a single em-dash fails the house check.

---

## 3. Pricing — exact source of truth (do not round or invent)

| Plan | Price | Allowance | Watermark | Notes |
|------|-------|-----------|-----------|-------|
| **Free** | **$0** | **2 videos / mo (120 min)** | **Yes** | full **1080p**, full editor, every caption style, no card |
| **Starter** | **$15/mo** | **10 videos / mo (600 min)** | No | no watermark, all ratios |
| **Pro** ⭐ | **$35/mo** | **30 videos / mo (1,800 min)** | No | + priority processing + early access (Recommended) |
| **Pay-as-you-go** | **$3 / video** | 1 video, never expires | No | top up anytime |

1 video = up to 60 minutes of source (a 90-min upload counts as 1.5). The only difference on Free is a
**visible watermark**, not a resolution downgrade. PAYG is **$3** everywhere (the original brief had a
stale "$2" in one FAQ answer — this build uses $3). CTAs: Free → "Start free", Starter → "Choose
Starter", Pro → "Choose Pro".

---

## 4. Copy I rewrote vs the brief's reference copy (and why)

- **Hero subhead** shortened to ≤20 words so the fold fits the viewport; the fuller version lives in the
  Demo section.
- **Em-dashes removed** from all reference copy (the brief had several, e.g. "land — a hook") → colons /
  periods. The page `<title>` also uses a colon, not an em-dash.
- **PAYG $2 → $3** everywhere.
- **Comparison** gained a "scored too low to ship" proof block (illustrative rejected scores) to make the
  wedge visual; the head-to-head keeps the brief's exact value props.
- **Section eyebrows** dropped the `// ` code-comment prefix (it read as junk) and became a coral-tick
  kicker; the hero eyebrow was removed entirely (its fold already carries two coral marks).
- **Two video clips' hooks** rewritten to match their real burned-in hook plates (see Honesty notes).

Everything else preserves the brief's value props, voice, and the required funnel blocks.
