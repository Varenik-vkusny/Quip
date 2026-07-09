-- 0017_studio_plan.sql — разрешить тариф 'studio' в profiles.plan.
--
-- Применить: Supabase Dashboard → SQL Editor → Run (ИЛИ через MCP apply_migration). Идемпотентна.
--
-- ЗАЧЕМ: код Studio ($49/мес, 42 видео) уже отгружен целиком — billing.PLANS['studio'],
-- Polar-вебхук (маппит по metadata.plan='studio'), фронт (lib/plans.ts + чекаут). ЕДИНСТВЕННЫЙ
-- блокер: CHECK-констрейнт из 0001 разрешает только ('free','starter','pro'). Без этой миграции
-- реальная оплата Studio проходит в Polar, но вебхук `set_user_plan(user, 'studio')' падает на
-- нарушении констрейнта (Postgres 23514 → HTTP 400) — деньги списаны, план НЕ проставлен.
--
-- БЕЗОПАСНОСТЬ: только расширяет допустимое множество значений. Существующие строки
-- ('free'/'starter'/'pro') остаются валидны; ничего не переписывается. plan по-прежнему пишет
-- ТОЛЬКО сервер (service-role) — self-upgrade невозможен.
--
-- ИМЯ КОНСТРЕЙНТА: инлайновый check из 0001 Postgres назвал автоматически `profiles_plan_check`
-- (стандарт <таблица>_<колонка>_check). Если у тебя он назван иначе — найди его так:
--   select conname from pg_constraint
--   where conrelid = 'public.profiles'::regclass and contype = 'c';
-- и подставь имя в DROP ниже.

alter table public.profiles drop constraint if exists profiles_plan_check;

alter table public.profiles
  add constraint profiles_plan_check
  check (plan in ('free', 'starter', 'pro', 'studio'));
