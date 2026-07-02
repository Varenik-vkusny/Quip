-- 0015_social_posting.sql — ДРЕМЛЮЩИЙ бэкенд автопостинга (social_accounts / post_schedules / post_jobs).
--
-- Применить: Supabase Dashboard → SQL Editor → Run (ИЛИ через MCP apply_migration). Идемпотентна.
--
-- ЗАЧЕМ: таблицы под автопостинг клипов в TikTok/IG/YouTube от имени подключённых юзеров по расписанию
-- (см. docs/SOCIAL_AUTOPOSTING_APPROVALS.md, app/social/). Эта миграция только СОЗДАЁТ таблицы — фича
-- остаётся инертной (код в app/social/ ничто не импортит, Modal-cron не зарегистрирован, флаг
-- SOCIAL_POSTING_ENABLED по умолчанию off). Можно накатить заранее, прод не меняется.
--
-- БЕЗОПАСНОСТЬ: RLS включён БЕЗ политик = deny-all для anon/authenticated (как 0009_agent_runs_rls).
-- Воркер ходит через service_role (обходит RLS). Это КРИТИЧНО для social_accounts.refresh_token_enc —
-- зашифрованный refresh-токен НИКОГДА не должен утекать через публичный anon-ключ PostgREST. Политики
-- own-row для чтения дашбордом (если фронт будет читать напрямую) добавим при разводке UI отдельной
-- миграцией; пока весь доступ — только через воркер.

-- ── подключённые соц-аккаунты (+ зашифрованный refresh-токен) ──
create table if not exists public.social_accounts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null,
    platform text not null check (platform in ('youtube', 'instagram', 'tiktok')),
    external_id text,                                  -- channel id / ig-user-id / open_id
    display_name text,
    refresh_token_enc text not null,                   -- Fernet-шифр (app/social/tokens.py)
    needs_reconnect boolean not null default false,    -- ставим при invalid_grant на refresh
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, platform, external_id)
);

-- ── расписание раскладки клипов (drip): слоты/день, таймзона, дневной кап ──
create table if not exists public.post_schedules (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null,
    account_id uuid not null references public.social_accounts(id) on delete cascade,
    name text,
    slot_times text[] not null default '{}',           -- напр. {'09:00','15:00','21:00'} (local)
    tz text not null default 'UTC',
    daily_cap int,                                     -- опц. кап постов/день (≤ числу слотов)
    created_at timestamptz not null default now()
);

-- ── конкретная публикация одного клипа в один аккаунт в момент due_at ──
create table if not exists public.post_jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null,
    account_id uuid not null references public.social_accounts(id) on delete cascade,
    platform text not null check (platform in ('youtube', 'instagram', 'tiktok')),
    clip_ref text not null,                            -- r2://<key> маркер (storage.resolve_media_url)
    caption text not null default '',
    due_at timestamptz not null,
    status text not null default 'pending'
        check (status in ('pending', 'publishing', 'published', 'failed', 'skipped')),
    remote_id text,                                    -- id поста на площадке после публикации
    error text,
    idempotency_key text unique,                       -- чтобы один клип не ушёл дважды
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- индексы под раннер: «что пора публиковать» + «сколько опубликовано сегодня этим аккаунтом»
create index if not exists post_jobs_due_idx on public.post_jobs (status, due_at);
create index if not exists post_jobs_acct_status_idx on public.post_jobs (account_id, status, updated_at);

-- ── авто-обновление updated_at на UPDATE (published_count_today фильтрует updated_at >= полночь UTC) ──
create or replace function public.social_touch_updated_at() returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists post_jobs_touch_updated on public.post_jobs;
create trigger post_jobs_touch_updated before update on public.post_jobs
    for each row execute function public.social_touch_updated_at();

drop trigger if exists social_accounts_touch_updated on public.social_accounts;
create trigger social_accounts_touch_updated before update on public.social_accounts
    for each row execute function public.social_touch_updated_at();

-- ── RLS: deny-all для anon/authenticated; доступ только через service_role воркера ──
alter table public.social_accounts enable row level security;
alter table public.post_schedules enable row level security;
alter table public.post_jobs enable row level security;
