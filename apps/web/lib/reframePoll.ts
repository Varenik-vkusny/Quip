import type { ReframeRegion, ReframeResult } from "./api";

// ────────────────────────────────────────────────────────────────────────────
// reframePoll — БЮДЖЕТ и ЦИКЛ поллинга GET /jobs/{job}/clips/{clip}/reframe.
//
// Контракт эндпоинта: 200 ready · 202 computing (сервер запустил ФОНОВЫЙ Cloud Run Job —
// поллим тот же GET) · 204 absent (только при compute=0) · 503 → расчёт реально упал.
//
// ⚠️ ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ setTimeout В КОМПОНЕНТЕ (баг, найденный ревью 2026-07-14):
// поллинг 202 не имел НИ лимита попыток, НИ дедлайна, НИ терминального состояния. Если запекание
// плана падало на сервере насовсем (битый source / кодек, на котором давится детектор шотов),
// сервер отдавал 202 БЕСКОНЕЧНО, и:
//   • полоса шотов навсегда залипала в «Detecting shots…»;
//   • честный failed-UI (FitTimeline `failed` + «Повторить») был НЕДОСТИЖИМ — 202 не ошибка;
//   • у серверного гарда пере-запуска TTL = 300 с, поэтому КАЖДАЯ открытая вкладка раз в 5 минут
//     заказывала НОВЫЙ Cloud Run Job (8 vCPU / 32 GiB) — вечно, на одном битом клипе.
//
// Отсюда два числа, а не «на глаз»:
//   • REFRAME_DEADLINE_MS = 300 000 — РОВНО серверный TTL гарда пере-запуска. Реальное запекание
//     измерено в 80–195 с, так что дедлайн с запасом их перекрывает; при этом мы НИКОГДА не шлём
//     GET на отметке ≥ TTL (см. `elapsed + delayMs >= REFRAME_DEADLINE_MS` в nextReframePoll) →
//     одна открытая вкладка = максимум ОДИН заказанный Job, второго спавна физически не будет.
//   • REFRAME_MAX_ATTEMPTS — ремень поверх подтяжек: дедлайн считается по стенным часам, а часы
//     могут «замереть»/уехать назад (сон ноутбука, коррекция NTP). Счётчик попыток от времени не
//     зависит и добьёт цикл в любом случае.
//
// Лестница пауз — как в `lib/useJob.ts` (2.5с → 5с → 10с + медленный такт для скрытой вкладки):
// первые полминуты держим отзывчивость (кэш мог прогреться), дальше расширяемся, чтобы не долбить
// API 120 раз за один расчёт.
//
// Скрытая вкладка: НЕ останавливаем цикл полностью, а замедляем до REFRAME_HIDDEN_POLL_MS. Полная
// остановка при тикающем стенном дедлайне сфабриковала бы ЛОЖНЫЙ таймаут (вернулся через 5 минут —
// «упало», хотя план давно готов). Замедлились + при возврате на вкладку дёргаем pollNow() → сразу
// свежий статус (тот же приём, что «online» в useJob).
// ────────────────────────────────────────────────────────────────────────────

/** Первый такт: план часто готов почти сразу (тёплый кэш регионов) — отвечаем быстро. */
export const REFRAME_POLL_MS = 2500;
/** Второй такт (после REFRAME_FAST_UNTIL_MS): расчёт явно идёт по-настоящему. */
export const REFRAME_POLL_MID_MS = 5000;
/** Потолок такта (после REFRAME_MID_UNTIL_MS) — как MAX_BACKOFF_MS в useJob. */
export const REFRAME_POLL_SLOW_MS = 10_000;
/** Скрытая вкладка: юзер на неё не смотрит — незачем дёргать API чаще. */
export const REFRAME_HIDDEN_POLL_MS = 15_000;

export const REFRAME_FAST_UNTIL_MS = 30_000;
export const REFRAME_MID_UNTIL_MS = 90_000;

/** Стенной дедлайн цикла = серверный TTL гарда пере-запуска: ни один GET не уйдёт на ≥ 300 с,
 *  значит вкладка не закажет второй Cloud Run Job. Запекание измерено в 80–195 с → запас ≥ 1.5×. */
export const REFRAME_DEADLINE_MS = 300_000;

/** Ремень поверх подтяжек: дедлайн зависит от стенных часов, счётчик попыток — нет. */
export const REFRAME_MAX_ATTEMPTS = 60;

/** Чем именно кончился расчёт плана (оба ведут в ОДНО честное failed-состояние + «Повторить»). */
export type ReframeFailure = "error" | "timeout";

/** Бюджет ОДНОГО цикла поллинга. Иммутабелен: nextReframePoll возвращает новый. */
export interface ReframePollBudget {
  readonly startedAt: number;
  readonly attempts: number;
}

export type ReframePollStep =
  | { kind: "poll"; delayMs: number; budget: ReframePollBudget }
  | { kind: "give-up"; reason: "deadline" | "attempts" };

/** Новый бюджет = сброс счётчика и дедлайна. «Повторить» просто начинает новый цикл. */
export function startReframePoll(now: number): ReframePollBudget {
  return { startedAt: now, attempts: 0 };
}

/** Пауза до следующего GET по прошедшему времени (и по видимости вкладки). PURE. */
export function reframePollDelayMs(elapsedMs: number, hidden = false): number {
  if (hidden) return REFRAME_HIDDEN_POLL_MS;
  if (elapsedMs < REFRAME_FAST_UNTIL_MS) return REFRAME_POLL_MS;
  if (elapsedMs < REFRAME_MID_UNTIL_MS) return REFRAME_POLL_MID_MS;
  return REFRAME_POLL_SLOW_MS;
}

/**
 * Решение о СЛЕДУЮЩЕМ такте поллинга: ждать `delayMs` — или сдаться (терминальный таймаут).
 * PURE (часы/видимость приходят аргументами).
 *
 * Сдаёмся, если (а) исчерпан лимит попыток, либо (б) следующий GET ушёл бы НА или ЗА дедлайн —
 * проверяем `elapsed + delayMs`, а не `elapsed`, ровно чтобы не отправить запрос, который на
 * сервере уже считается «гард протух» и заказывает новый Job.
 */
export function nextReframePoll(
  budget: ReframePollBudget,
  opts: { now: number; hidden?: boolean },
): ReframePollStep {
  if (budget.attempts >= REFRAME_MAX_ATTEMPTS) return { kind: "give-up", reason: "attempts" };
  const elapsed = Math.max(0, opts.now - budget.startedAt);
  const delayMs = reframePollDelayMs(elapsed, opts.hidden);
  if (elapsed + delayMs >= REFRAME_DEADLINE_MS) return { kind: "give-up", reason: "deadline" };
  return {
    kind: "poll",
    delayMs,
    budget: { startedAt: budget.startedAt, attempts: budget.attempts + 1 },
  };
}

/**
 * Как реагировать на ИСКЛЮЧЕНИЕ из getClipReframe.
 * • AbortError — общий (дедуплицированный) запрос отменил ДРУГОЙ потребитель, наш цикл жив: это не
 *   сбой расчёта → переспрашиваем (но ЗА СЧЁТ БЮДЖЕТА — иначе получаем второй вечный цикл).
 * • Всё остальное (503 «bake failed», 5xx, сломанный контракт 200-без-regions) — ТЕРМИНАЛЬНО.
 *   Никаких тихих фолбэков: честная ошибка + «Повторить».
 */
export function classifyReframeError(err: unknown): "retry" | "fail" {
  return (err as { name?: string } | null)?.name === "AbortError" ? "retry" : "fail";
}

export interface ReframePollDeps {
  /** Один GET /reframe (у вызывающего — уже с abort-сигналом и in-flight дедупом). */
  fetchPlan: () => Promise<ReframeResult>;
  /** План готов — терминально, цикл остановлен. */
  onReady: (regions: ReframeRegion[]) => void;
  /** Расчёт кончился ничем — терминально: честный failed-UI + «Повторить». НИКОГДА не молча. */
  onFailed: (kind: ReframeFailure, err?: unknown) => void;
  /** Цикл ещё актуален? (seq-гард + тот ли клип). false → тихо выходим, без onFailed. */
  alive: () => boolean;
  hidden?: () => boolean;
  now?: () => number;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

export interface ReframePollHandle {
  /** Немедленно переспросить статус, не досиживая паузу (вкладку вернули на передний план). */
  pollNow: () => void;
  /** Остановить цикл и снять ОТЛОЖЕННЫЙ таймер (смена клипа / unmount / новый loadReframe). */
  stop: () => void;
  /** Резолвится, когда ПЕРВЫЙ GET получил ответ (ready/computing/ошибка) — НЕ ждёт конца поллинга.
   *  Нужен вызывающим, которые держат оверлей «Recomputing framing…» до первого ответа сервера.
   *  Никогда не реджектится: ошибка уже ушла в onFailed. */
  firstAttempt: Promise<void>;
}

/**
 * Цикл поллинга /reframe: первый GET сразу, 202 → повтор по лестнице пауз, ready/absent/ошибка/
 * исчерпанный бюджет → ровно ОДИН терминальный колбэк. Все зависимости (часы, таймеры, видимость)
 * инжектируются → цикл тестируется целиком, без React и без реального времени.
 */
export function runReframePoll(deps: ReframePollDeps): ReframePollHandle {
  const now = deps.now ?? (() => Date.now());
  const hidden = deps.hidden ?? (() => false);
  const schedule =
    deps.schedule ?? ((fn: () => void, ms: number) => setTimeout(fn, ms) as unknown);
  const cancel = deps.cancel ?? ((h: unknown) => clearTimeout(h as ReturnType<typeof setTimeout>));

  let budget = startReframePoll(now());
  let timer: unknown = null;
  let done = false; // терминальное состояние ИЛИ внешний stop() — больше ни одного GET

  const clearPending = () => {
    if (timer !== null) {
      cancel(timer);
      timer = null;
    }
  };
  const live = () => !done && deps.alive();
  const finish = (fn: () => void) => {
    done = true;
    clearPending();
    fn();
  };

  function later(delayMs: number): void {
    clearPending();
    timer = schedule(() => {
      timer = null;
      if (live()) void attempt();
    }, delayMs);
  }

  /** 202 → следующий такт ЗА СЧЁТ БЮДЖЕТА. Бюджет кончился → терминальный таймаут (не тишина). */
  function again(): void {
    const step = nextReframePoll(budget, { now: now(), hidden: hidden() });
    if (step.kind === "give-up") {
      finish(() => deps.onFailed("timeout", step.reason));
      return;
    }
    budget = step.budget;
    later(step.delayMs);
  }

  async function attempt(): Promise<void> {
    try {
      const res = await deps.fetchPlan();
      if (!live()) return; // клип сменился / stop() — результат никому не нужен
      if (res.status === "computing") {
        again();
        return;
      }
      if (res.status === "absent") {
        // compute=1 не должен возвращать 204: это не «пустой план», а сломанный контракт.
        finish(() => deps.onFailed("error", new Error("reframe: 204 absent with compute=1")));
        return;
      }
      const regions = res.regions;
      finish(() => deps.onReady(regions));
    } catch (e) {
      if (!live()) return;
      if (classifyReframeError(e) === "retry") {
        again(); // чужой abort общего промиса — переспрашиваем, но бюджет всё равно тратим
        return;
      }
      finish(() => deps.onFailed("error", e)); // 503 и пр. — ТЕРМИНАЛЬНО
    }
  }

  const firstAttempt = attempt(); // первый GET — сразу, без паузы

  return {
    firstAttempt,
    pollNow: () => {
      if (!live()) return;
      later(0);
    },
    stop: () => {
      done = true;
      clearPending();
    },
  };
}
