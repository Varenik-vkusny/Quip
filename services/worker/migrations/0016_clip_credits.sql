-- 0016_clip_credits.sql — баланс клип-кредитов для «извлечь лишний клип» (Supabase Postgres).
--
-- Применить ПОСЛЕ 0002_credits.sql (Dashboard → SQL Editor → Run, или supabase db push).
-- Идемпотентна (IF NOT EXISTS) — повторный прогон безопасен.
--
-- Клип-кредит — ОТДЕЛЬНЫЙ от PAYG баланс. Тратится ТОЛЬКО на извлечение нового клипа из
-- неотобранного момента ЗА free-cap (app/billing.extract_requires_payment; app/main.extract_clip
-- вызывает db.consume_clip_credit). НЕ путать с profiles.payg_credits (пул на обработку целого
-- видео, $3/кредит). Разделение колонок закрывает эксплойт «$1-клип → $3-видео»: вебхук Polar
-- начисляет сюда ТОЛЬКО заказ продукта POLAR_PRODUCT_EXTRA_CLIP ($1), в payg_credits — ТОЛЬКО
-- заказ POLAR_PRODUCT_PAYG ($3); матч строго по product_id (app/polar.parse_extra_clip_order).
--
-- Пишет ТОЛЬКО сервер (service-role): вебхук $1-заказа начисляет (add_clip_credits), эндпоинт
-- извлечения списывает по одному (consume_clip_credit — условный decrement при балансе > 0).

alter table public.profiles
  add column if not exists clip_credits integer not null default 0;
