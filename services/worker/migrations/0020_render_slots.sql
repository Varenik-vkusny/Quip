-- 0020_render_slots.sql — ГЛОБАЛЬНЫЙ бюджет одновременных рендер-контейнеров (Фаза 1c,
-- spec docs/superpowers/specs/2026-07-15-multiuser-scaling-design.md).
--
-- Применить в Supabase (Dashboard -> SQL Editor -> Run) ДО деплоя воркера, который задаёт
-- RENDER_GLOBAL_BUDGET. Идемпотентна. Пока RENDER_GLOBAL_BUDGET не задан — код эти RPC НЕ вызывает
-- (kill-switch off), так что накатить миграцию можно заранее, без риска для текущего прода.
--
-- ЗАЧЕМ: окно фан-аута RENDER_FANOUT_MAX — ПЕР-ВИДЕО (свой координатор на джобу). N одновременных
-- джоб (N юзеров) => суммарно до N×окна рендер-контейнеров: большой аплоад забивает пул, мелкий
-- голодает, всплеск пробивает региональные квоты. Общий счётчик слотов делает потолок ГЛОБАЛЬНЫМ:
-- суммарно одновременно <= p_max рендеров по ВСЕМ джобам (каждый координатор берёт слот перед
-- спавном, освобождает на терминале).
--
-- ПОЧЕМУ СТРОКА-НА-СЛОТ, а не один integer: строка несёт acquired_at => возможен TTL-reclaim.
-- Контейнер может УМЕРЕТЬ (OOM/kill) не освободив слот; без реклейма счётчик медленно дедлочит
-- бюджет. acquire сперва удаляет протухшие (> p_ttl_sec) строки, потом считает. Integer так не умеет.
--
-- АТОМАРНОСТЬ: две гонки acquire могли бы обе увидеть count<max и обе вставить (over-admission).
-- pg_advisory_xact_lock сериализует acquire'ы (reclaim+count+insert атомарны). Release — DELETE по id,
-- лок не нужен. SECURITY DEFINER + service_role: слоты пишет ТОЛЬКО сервер (как весь cloud_state).
-- Возвращаемые значения явные (uuid / NULL / row_count) — без тихих фолбэков (правило №8).

create table if not exists public.render_slots (
    id uuid primary key default gen_random_uuid(),
    job_id text,
    acquired_at timestamptz not null default now()
);

create index if not exists render_slots_acquired_at_idx on public.render_slots (acquired_at);


create or replace function public.render_slot_acquire(
    p_max int,
    p_ttl_sec int default 2400,
    p_job_id text default null
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    slot_id uuid;
    cur int;
begin
    -- сериализуем acquire'ы: reclaim+count+insert атомарны относительно других acquire
    perform pg_advisory_xact_lock(hashtext('quip_render_slots'));
    -- реклейм: слот, не освобождённый дольше TTL, принадлежал умершему контейнеру
    delete from public.render_slots
     where acquired_at < now() - make_interval(secs => greatest(p_ttl_sec, 1));
    select count(*) into cur from public.render_slots;
    if cur >= p_max then
        return null;  -- бюджет полон: координатор подождёт и повторит
    end if;
    insert into public.render_slots (job_id) values (p_job_id) returning id into slot_id;
    return slot_id;
end;
$$;


create or replace function public.render_slot_release(p_slot_id uuid)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
    n int;
begin
    delete from public.render_slots where id = p_slot_id;
    get diagnostics n = row_count;
    return n;  -- 0 = слот уже реклеймнут TTL (безвредно; release идемпотентен)
end;
$$;


grant execute on function public.render_slot_acquire(int, int, text) to service_role;
grant execute on function public.render_slot_release(uuid) to service_role;
