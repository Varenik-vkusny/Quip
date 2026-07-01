-- 0017_studio_plan.sql — добавить план 'studio' в CHECK profiles.plan. Аддитивно/безопасно для
-- free/starter/pro. ПРИМЕНИТЬ ДО деплоя воркера и создания Polar-продукта (иначе subscription.created
-- для studio → set_user_plan('studio') → PostgREST 400 → вебхук 500 в цикле, клиент не провижнится).
alter table public.profiles drop constraint if exists profiles_plan_check;
alter table public.profiles
  add constraint profiles_plan_check check (plan in ('free', 'starter', 'pro', 'studio'));
