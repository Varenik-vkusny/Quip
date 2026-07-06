# Assets — manifest and the ffmpeg pipeline

All media is already web-optimized and lives under `public/`. This doc lists every asset and the exact
commands used to produce it, so you can regenerate, recompress, or swap clips.

---

## 1. Manifest

### `public/media/` — the pipeline demo

| File | Dimensions | ~Size | Used by | Notes |
|------|-----------|-------|---------|-------|
| `demo.mp4` | 1600×822 (16:8.2), 57s | 2.3 MB | `sections/Demo.tsx` | the real upload→moments→clips run; **muted, no audio track**, faststart, plays in view |
| `demo-poster.jpg` | 1876×964 | 0.12 MB | Demo poster + OG/Twitter image | also referenced by `layout.tsx` metadata |

### `public/clips/` — real captioned 9:16 output

| File | Dimensions | ~Size | Used by | Notes |
|------|-----------|-------|---------|-------|
| `clip-1.mp4` | 540×960 (9:16), 58s | 2.0 MB | Hero readout + Comparison "made the cut" | real clip; hook plate "McConaughey's dad's surprising reaction to quitting law school" |
| `clip-2.mp4` | 540×960, 47s | 1.7 MB | WhyQuip anatomy card | real clip; hook plate "How Matthew McConaughey earned his dad's support" |
| `poster-1.jpg` | 540×960 | 35 KB | poster for clip-1 + Craft "captions" still | |
| `poster-2.jpg` | 540×960 | 32 KB | poster for clip-2 + Craft "captions" still | |
| `frame-1.jpg` | 480×854 | 36 KB | `CLIPS[2]` evidence card still | distinct still |
| `frame-5.jpg` | 480×854 | 28 KB | `CLIPS[3]` evidence card still | distinct still |

### `public/brand/` + `app/` icons

| File | Use |
|------|-----|
| `brand/icon.png` (512²), `app/icon.png` | the lens-"Q" + coral play-triangle mark; Next metadata icon |
| `brand/apple-icon.png`, `app/apple-icon.png` | apple-touch icon |
| `brand/favicon.ico`, `app/favicon.ico` | browser favicon |

> The logo lockup in the nav/footer is **not** an image — it is composed in `sections/Nav.tsx` (`Logo`):
> a coral rounded square + a Phosphor `Play` glyph + the "Quip" wordmark in Onest 800. Refine there.

---

## 2. The ffmpeg pipeline (how these were made)

Sources were the originals shipped in the brief kit: `assets/demo/pipeline-demo.mp4`,
`assets/demo/clip_02-captioned.mp4` (→ `clip-1`), `assets/demo/clip_03-captioned.mp4` (→ `clip-2`).
The raw captioned clips were ~21–26 MB each; these commands bring them to web weight.

```bash
# Demo video: scale to 1600w, strip audio, H.264, faststart (~2.3 MB)
ffmpeg -y -i pipeline-demo.mp4 \
  -vf "scale=1600:-2" -an -c:v libx264 -preset slow -crf 25 \
  -pix_fmt yuv420p -movflags +faststart  demo.mp4

# A vertical output clip: scale to 540w, strip audio, faststart (~1.7–2.0 MB)
ffmpeg -y -i clip_02-captioned.mp4 \
  -vf "scale=540:-2" -an -c:v libx264 -preset slow -crf 30 \
  -pix_fmt yuv420p -movflags +faststart  clip-1.mp4

# A poster frame (grab at t=2s)
ffmpeg -y -ss 2 -i clip_02-captioned.mp4 -vf "scale=540:-2" -frames:v 1 -q:v 3  poster-1.jpg

# A distinct still for an evidence card (grab at any timestamp T)
ffmpeg -y -ss 4 -i clip_02-captioned.mp4 -vf "scale=480:-2" -frames:v 1 -q:v 3  frame-1.jpg
```

Knobs: raise `-crf` for smaller files (28–32 is fine for these), lower for higher quality. `-an` drops
audio (the page never plays sound). `+faststart` moves the moov atom to the front so playback starts
before the full file downloads. `scale=W:-2` keeps aspect and forces an even height (H.264 requirement).

## 3. Swapping a clip (recipe)

1. Compress the new vertical clip to `public/clips/clip-N.mp4` (540w command above).
2. Grab a poster: `... -ss 2 ... -frames:v 1 -q:v 3 poster-N.jpg`.
3. In `lib/content.ts`, point the relevant `Clip` at the new `src`/`poster` **and rewrite its `hook`,
   `reason`, and `type` to match the new clip's real content / burned-in hook plate** (see
   `docs/CONTENT.md` → Honesty notes — a card must never contradict the hook plate visible in its video).
4. `npm run build` and eyeball the section.

## 4. Deploy reminder

Ensure your host does not strip these on deploy. On Vercel specifically, a `.vercelignore` line like
`*.mp4` matches **recursively** and will drop `media/demo.mp4` and the clips, leaving broken video. Use
`/*.mp4` (root-anchored) or list paths explicitly, then confirm all three mp4s return HTTP 200 on the
deployed URL.
