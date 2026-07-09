# 2026-07-09 — Ночной ран: редизайн «Studio» / Signal Gold

> Архивный отчёт (история). Актуальная система — `DESIGN.md` + `apps/web/app/globals.css`.
> Ветка `feat/premium-redesign` (от `feat/video-map-cockpit`). НЕ задеплоено.

## Зачем
Фаундер: «чёрно-оранжевый не выглядит премиально, за него не хочется платить»; нужен
«живой» лендинг уровня usefastlane.ai с уникальным анимированным элементом; две темы.

## Что решено (утверждено фаундером интерактивно)
1. **Signal Gold** — брендовое золото только на хроме; коралл = только hook-момент.
2. **Светлый маркетинг / тёмное приложение** (пер-поверхностные дефолты, выбор юзера
   в localStorage побеждает везде). Cookie НЕ используется — SSG маркетинга сохранён.
3. Редактор: хром в обеих темах, видео-канвас всегда тёмный.
4. Бегущие строки (marquee) запрещены как «ИИшность».

## Коммиты (все с зелёным `just check`)
1. `chore: sync shipped free-plan reality` — хвост отгруженного 1-video-Free в ветку.
2. `feat(design): dual-theme token architecture` — токены `:root`/`[data-theme=dark]` +
   `@theme inline`; pre-paint скрипт; ThemeApplier/ThemeToggle; шрифтовой ретюн (650/−2.8%);
   gsap+lenis.
3. `feat(landing): Studio redesign v1` — Hero (веер реальных клипов + reasoning-карточка
   + count-up + параллакс), **WaveformRiver** (детерминированная canvas-волна, золотые
   вспышки моментов, чипы типов, тема-реактивность, reduced-motion статик), ScrollFx
   (Lenis + пин «Карты моментов» с горизонтальным проездом, манифест-скраб, 3D-вход окна),
   Nav pill-морф, StudioWindow через вложенный `data-theme="dark"`, новые секции контента
   RU+EN в `landingContent.ts`.
4. `feat(design): token sweep` — 4 параллельных агента: анти-слоп рестайл 9 легаси-секций
   (eyebrows/mono-caps/глоу — прочь; Pricing в новом костюме, данные планов не тронуты);
   ClipCard/ClipGrid/dashboard токен-чистые (TONE_GLOW удалён, скоринг ok/warn/bad);
   редактор-хром двухтемный (исключения: цвета НА видео + цвета вывода субтитров);
   ui/marketing свип + ThemeToggle в MarketingNav/AppHeader. OG + DESIGN.md в золото.
5. `polish(landing)` — хедлайн ужат в обоих языках (2 строки, balance), 24 RU-строки без
   em-dash, общий `ui/Logo` → золотой SVG-знак, фикс прямого захода на #якорь (пины
   пересчитываются на load, Lenis re-aim).
6. Иконки: `app/icon.png`, `apple-icon.png`, `favicon.ico`, `public/icon.png`,
   `public/logo-88.png` перегенерены в золото (canvas → ffmpeg).

## Верификация
Живой Chrome-проход (dev): лендинг светлая/тёмная EN/RU (hero, волна, pill-nav, карта-пин,
манифест, окно, прайсинг, FAQ, финал, футер), `/pricing`, `/dashboard` светлая+тёмная RU;
консоль чистая; `#pricing` прямой заход починен. 185 vitest + tsc/eslint/ruff на каждом коммите.

## Уроки/грабли (важное — в memory `premium-redesign-studio-gold`)
- Session limit убивает саб-агентов ПОСРЕДИ правки → resume тем же agentId через
  SendMessage + ручная проверка полу-применённых правок (ClipCard имел битые ссылки).
- Runtime innerHTML-сплит слов ломает вложенные span (манифест) → слова размечать в JSX.
- Прямой заход на #hash при пинах = попадание в pin-spacer → ScrollTrigger.refresh на load.
- `Stop-Process -Name node` = смерть MCP-серверов; только TaskStop по id.
- Chrome-скриншоты запиненных секций при DPR≠1 врут — проверять геометрией.

## Follow-ups (утро)
- `/pricing` и use-case страницы: анти-слоп рестайл до паритета с лендингом (mono-ярлыки
  PLAN/MONTHLY LIMIT ещё старые).
- Мобильный проход (веер hero, пины) + Lighthouse.
- Editor визуальный QA на реальном клипе (хром перекрашен, но глазами не смотрели).
- Founder: посмотреть обе темы, RU/EN; решение о деплое (Vercel auto с main; ветку мержить
  после ревью).
- worker hook-плашка в клипах остаётся коралловой (продуктовый вывод — менять ли, решает
  фаундер отдельно).
