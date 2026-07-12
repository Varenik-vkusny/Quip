-- 0016_job_remove_pauses.sql — per-job «убирать паузы» тумблер (global opt-in на загрузке).
--
-- Применить в Supabase (Dashboard → SQL Editor → Run) ИЛИ через MCP apply_migration. Идемпотентна.
--
-- ЗАЧЕМ: юзер на загрузке может включить удаление пауз для ВСЕГО видео (дефолт OFF, opt-in). Флаг
-- ставится на создании джобы (эндпоинты create_job/upload-complete/check-duplicate), а воркер при
-- сборке дефолтного ClipEdit (ensure_edit) читает его у владельца джобы и сидит silence_config
-- (enabled=true) на КАЖДЫЙ клип. Per-clip тумблер в редакторе переопределяет (clip_edits).
-- Хранится на jobs (per-video настройка обработки), НЕ на profiles (это выбор ЭТОЙ загрузки).
--
-- service_role пишет (insert_job) / читает (ensure_edit). RLS не меняем — jobs уже own-scoped,
-- сервер ходит service-role'ом в обход RLS (как cancellable/user_id).

alter table public.jobs add column if not exists remove_pauses boolean not null default false;
