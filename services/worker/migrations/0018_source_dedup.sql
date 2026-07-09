-- 0018_source_dedup.sql — per-user дедуп исходников (Supabase Postgres).
--
-- Применить: Supabase Dashboard → SQL Editor → Run (ИЛИ через MCP apply_migration). Идемпотентна
-- (IF NOT EXISTS) — повторный прогон безопасен.
--
-- ЗАЧЕМ: когда юзер загружает видео БАЙТ-В-БАЙТ идентичное тому, что ОН уже грузил, незачем
-- гонять байты в R2 повторно — переиспользуем уже лежащий source (server-side R2 copy) и сразу
-- запускаем обработку. Клиент считает отпечаток ``<size>:<sha256-hex>`` и спрашивает
-- POST /jobs/check-duplicate; сервер ищет свою прошлую джобу с тем же отпечатком.
--
-- source_fingerprint пишет ТОЛЬКО сервер (service-role) в upload-complete/check-duplicate.
--
-- БЕЗОПАСНОСТЬ (важно): дедуп СТРОГО per-user. Индекс и все запросы фильтруют по (user_id,
-- source_fingerprint) вместе. Матч по отпечатку БЕЗ user_id был бы оракулом существования файла
-- у ЛЮБОГО юзера («ответил duplicate → у кого-то есть этот файл») — это IDOR-класс утечки,
-- которую команда только что закрыла на всех эндпоинтах. Поэтому per-user и никак иначе.
--
-- partial-индекс (WHERE source_fingerprint is not null): индексируем ТОЛЬКО строки с отпечатком
-- (старые/URL/youtube-джобы его не несут) → индекс компактный, лукап (user_id, fingerprint) быстр.

alter table public.jobs
  add column if not exists source_fingerprint text;

create index if not exists idx_jobs_user_fingerprint
  on public.jobs (user_id, source_fingerprint)
  where source_fingerprint is not null;
