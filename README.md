# Quip

**Live:** [quip.ink](https://quip.ink) · SaaS for podcast studios

Turn one long video or podcast episode into several **explainable** vertical clips: each with
burned-in captions, a hook, a confidence score, and a plain reason it works.

## Highlights

- **Built solo in 6 weeks** (Jun–Jul 2026): 670 commits, ~80,000 lines, 134 test files, mypy strict.
- **11.6× faster pipeline**: an 86-minute podcast went from 60.5 to 5.2 minutes; active-speaker
  detection **42× faster** after swapping S3FD for MediaPipe (measurements in `docs/BENCHMARKS.md`).
- **Production on Google Cloud Run** with container-per-clip fan-out, Supabase, Cloudflare R2,
  Polar billing and 21 database migrations.
- Diagnosed and fixed a production OOM/503 incident by moving heavy computer vision out of the web
  service into a separate job, capping concurrency and attaching a volume.

## Stack

| Layer | Tech |
|---|---|
| Frontend | Next.js 16, React 19, TypeScript |
| Worker | Python, FastAPI, MediaPipe, ffmpeg |
| Data and infra | Supabase (Postgres), Cloudflare R2, Google Cloud Run, Polar billing |

---
Turn one long video or podcast episode into several **explainable** vertical clips — each with
burned-in captions, a hook, a confidence score, and a plain reason it works.

Monorepo:
- `apps/web` — Next.js 16 frontend (Vercel `quip-app`, auto-deploys on `main`).
- `services/worker` — Python / FastAPI pipeline (Modal `quip-worker`).
- `packages/shared` — TypeScript types, **codegen** from `services/worker/app/models.py` (`just types`).

## 📚 Documentation — start here

**→ [`docs/README.md`](docs/README.md)** is the single index: the current production reality,
the reading order for a new session, what you must not break, and the deploy/infra map. Read it
first; everything else is linked from there.

## Run locally (Windows / PowerShell)

See `docs/HANDOFF.md` §3 — worker via `uv run uvicorn app.main:app`, web via
`pnpm --filter web dev`, pre-commit gate via `just check`. (Deploy/billing notes in HANDOFF are
outdated — trust `docs/README.md`.)
