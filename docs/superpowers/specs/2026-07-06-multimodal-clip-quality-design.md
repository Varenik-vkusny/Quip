# Multimodal Clip Quality (A→B) — Design

> Status: **Phase A + chunked selection IMPLEMENTED** (2026-07-06). `just check` green; verified
> locally on the 86-min Cardi B interview — selection went **2 → 10 clips** (spread 1:42 → 80:02),
> reframe flash-checked clean on the 9-shot clip_08. **Chunked selection was added during
> implementation (NOT in the original spec below)** — the single Gemini call collapsed recall on the
> long input (2 clips, both in the first 25 min), so selection now runs per-window (see `stage2_select.py`
> `should_chunk`/`make_windows`/`dedup_raw_segments`/`select_spread`, README "Умные границы", CORE §4.3).
> **Phase B (Gemini-vision window pass) still PENDING.** Original approval: founder chose target **B**,
> sequenced A→B.
> Owner: this session. Source of truth for the "kratno improve clip quality" initiative.
> Read `docs/README.md` + `docs/REFRAME_FPS_GRID_INVARIANT.md` before touching any code here.

## Problem (founder symptoms)

1. Clips end on a fragment / a stray filler ("uh/um/э/а") survives at start or end.
2. The thought isn't fully complete.
3. Weak attention-capture in the first ~2 s; hooks not reliably "viral".
4. Reframe still sometimes wrong (wrong subject / non-face / profile → blur-bars).

## Root cause (code-verified, 5-agent audit 2026-07-06)

- **Selection is 100% text-blind.** Gemini gets only the numbered transcript
  (`stage2_select.py:515`, `contents=user_prompt: str`). No frames, no audio.
- **A likely regression (2026-07-06, commit `18abe24`):** the deterministic boundary
  snappers `snap_start_index`/`snap_end_index` were removed from the live `postprocess`
  path (now only called in `editor/video_map.py`). `filler_words=true` now *injects*
  fillers as boundary-eligible words with nothing deterministic to trim them → symptoms
  (1)(2) get worse, not better.
- **Audio is already computed but firewalled.** RMS loudness + energy peaks
  (`preview_moments.py`) are explicitly kept out of selection (`run.py:623-624`).
- **Reframe ceiling is the perception layer.** MediaPipe `blaze_face_short_range`
  (~224 KB, near-frontal only) + LR-ASD (~3.4 MB, "is this face speaking"). Non-face /
  profile / whole-body subjects → `fit` blur-bars. The tracker output is a small clean
  contract `SpeakerTrack(f0,f1,cx,width,speak)` consumed only by `plan_regions` → a
  better subject signal can be injected at `score_tracks_in_segment` without touching
  the region planner, the renderer, or the Δ=0 grid.

## Verdict on "give Gemini the whole video" (numbers, agent 5)

**Not viable / not economical.** 3 h source = ~3.1M tokens (default res, > 1M window);
timestamp drift 5–10 min on long video; per-frame bbox driving is jittery + cost/latency
prohibitive; total cost 3–15× today's $0.16 and blows the "minutes" latency bar.
**The economical form of the idea = a single Gemini-vision pass over the already-chosen
candidate windows** (low-res + audio, source uploaded once via File API + startOffset).

## Non-goals

- No per-frame Gemini bbox driving of the crop (stays on tiny CPU models; Δ=0 intact).
- No whole-3h Gemini scan.
- Phase C items (video-aware full-source selection, NL "find X", SAM2/saliency non-face
  tracking, visual polish zoom/b-roll) are **roadmap**, not this spec.

---

## Phase A — Deterministic, audio-aware clean cutting (pure logic, $0 API)

All in `stage2_select.py` (+ `run.py` wiring). Pure functions, **TDD: failing test first**.

- **A1. Restore snappers** into `postprocess`, layered ON TOP of the 2026-07-06 additions
  (keep indexed transcript, `<pause>` tags, −150 ms clamped start pad). Re-add
  `si = snap_start_index(...)` / `ei = snap_end_index(..., start_idx=si)` before
  `indices_to_times`.
- **A2. New pure `refine_boundaries_audio(words, loudness, si, ei) -> (si, ei)`:**
  - trim leading/trailing **filler words** at the very edges using their timestamps;
  - end-snap ladder: sentence-final `.?!` → else largest pause ≥ 0.35 s → else nearest
    **low-energy (breath) trough** in the loudness curve;
  - start clamp off any leading filler.
- **A3. Un-firewall loudness into selection.** Thread the already-computed loudness curve
  from `run.py` into `select_segments`; A2 consumes it. Optionally annotate high-energy
  ("emphatic") words in the prompt to strengthen the opening. `preview_moments` keeps its
  cosmetic co-watch role too (do not break that invariant's UI purpose — just stop it
  being the *only* consumer).
- **Docs:** fix drift in the same change — `CORE §4.2` (`diarize=false`→`true`,
  `filler_words`), `§4.3` (snappers active again; `20 s`/`12` numbers), README "Умные
  границы", and remove/annotate vestigial `wide_speak_min` (`§4.4`).

## Phase B — Gemini-vision window pass (multimodal, +~$0.05/run, +~30 s)

New module `pipeline/stage2b_vision_review.py` (pure-ish; HTTP isolated behind a client).

- **Runs after `select_segments`, before reframe/render.** Upload source once (File API in
  cloud; local file path in dev). One call per candidate window using
  `videoMetadata.startOffset/endOffset`, `media_resolution=low`, audio on, model
  `gemini-2.5-flash`. Structured JSON per window:
  ```
  {
    clean_start_sec, clean_end_sec,      # refined edges (complete thought, no edge filler)
    is_complete_thought: bool,
    edge_filler_trimmed: bool,
    subject_to_follow: [{shot_idx, box_1000?|label}],   # who/what to keep in frame per shot
    fill_vs_fit: [{shot_idx, mode}],                    # per-shot prior
    hook_strength: float [0,1],          # visual+audio hook quality
    hook_reason: str
  }
  ```
- **Integration:**
  - *Cut:* deterministic layer (A) runs first as the $0 floor; Gemini **may override the
    edge but only within ±1 word/pause** of the deterministic boundary (guard against
    Gemini timestamp drift). [DECISION — founder default]
  - *Reframe:* map `subject_to_follow` to the nearest face track → set as locked target /
    tie-breaker in `_pick_target`; `fill_vs_fit` = per-shot prior. **Dense per-frame
    tracking stays on tiny models; Δ=0 untouched.** Any deploy that changes this →
    `update public.job_artifacts set reframe_regions = null;`.
  - *Ranking:* `hook_strength` → new score; clips sorted best-first + score shown in card.
    [DECISION — founder default: sort AND display]
- **No silent fallback (rule #8):** Gemini failure/timeout → fall back to deterministic-only
  edges + face-only reframe, **logged**, job still succeeds.
- **Dual-mode:** cloud = File API upload; local dev = pass the local source path / bytes.
- **Economics:** log added cost/latency to `runs.jsonl`; update `docs/BENCHMARKS.md`.

## Invariants to respect (do NOT break)

- **Δ=0 frame grid** — geometry stays in native fps; B only supplies *which subject* and
  *fill/fit*, never per-frame crop coordinates.
- **Two-layer reframe cache** — clear `reframe_regions` after any reframe-affecting deploy.
- **Type contract** — any `models.py` change → `just types`; never hand-edit `packages/shared`.
- **Commit gate** — `just check` green; TDD on all pure logic.

## Verification (MANDATORY — eyes, frame-by-frame, real render)

Test video: `C:\Users\user\Downloads\Telegram Desktop\CARDI B_ ... .mp4` (86.5 min, 720p,
29.97 fps, 16:9). For each milestone (baseline → A → B), render clips **with hook + burned
captions** and eyeball:
- edges: no "uh/um/э/а", clean in/out;
- complete thought;
- first ~2 s hook lands;
- reframe: no flashes, correct subject, no needless blur-bars.
Report exact clip file paths to the founder for independent review. Keep baseline vs A vs B
for comparison. Unit tests + numbers do NOT substitute for eyes (REFRAME invariant rule).

## Rollout / knobs

- `SELECT_AUDIO_AWARE` (A, default on), `VISION_REVIEW_ENABLED` (B, default on cloud),
  `VISION_REVIEW_MODEL=gemini-2.5-flash`, `VISION_REVIEW_MEDIA_RES=low`.
- Ship A first (verify on Cardi B), then B behind its flag.
