"""Dormant auto-posting backend (YouTube first; Instagram/TikTok later).

⚠️ NOT WIRED INTO THE LIVE APP. This package is pure backend LOGIC laid down ahead of the
platform approvals (OAuth verification / app review / quota audits). Nothing here is imported
by ``app.main`` and NO Modal cron is registered, so it is fully inert in production until the
founder passes approval and a future session wires the routes + scheduler cron.

Layout:
  config.py     — env-gated feature flags + secrets (mirrors app.cloud_state os.environ pattern)
  models.py     — internal value objects (Platform, PostStatus, PlannedPost, …) — NOT contract types
  scheduler.py  — PURE drip planner ("25 clips → 3/day at fixed slots, with caps")
  tokens.py     — OAuth token encryption-at-rest (Fernet, key from env)
  youtube.py    — YouTube Data API v3 client (OAuth + resumable videos.insert + publishAt)
  repo.py       — PostgREST persistence (mirrors app.cloud_state thin-httpx style)
  runner.py     — due-post dispatcher (injectable deps; reused by a future Modal cron)
"""
