-- 0016_payg_minutes.sql — PAYG становится делимым минутным балансом (было: целые кредиты с
-- округлением ВВЕРХ, терявшим остаток короткого PAYG-видео). Применить (Supabase SQL Editor /
-- db push) ПЕРЕД деплоем воркера; сразу после — modal deploy (окно транзита секунды). Идемпотентна.
alter table public.profiles
  add column if not exists payg_minutes numeric not null default 0;
-- Разовый бэкфилл: старые целые кредиты → минуты (1 кредит = 60 мин). Гард payg_minutes=0
-- защищает повторный прогон (после деплоя новый код пишет payg_minutes напрямую).
update public.profiles
  set payg_minutes = payg_credits * 60
  where payg_minutes = 0 and payg_credits > 0;
