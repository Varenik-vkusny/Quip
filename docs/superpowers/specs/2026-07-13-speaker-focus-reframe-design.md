# Speaker Focus — tight speaker-follow с cut-переключением (виртуальная вторая камера)

> **Статус:** DESIGN (одобрен основателем 2026-07-13). Источник ресёрча: `docs/archive/2026-07-13-active-speaker-reframe-research.md` + JOURNAL 2026-07-13.
> **Прайор-арт-прототип:** EditIQ (ACM IUI 2025, arXiv 2502.02172) — глубокий разбор в отчёте.
> **Инвариант, который НЕЛЬЗЯ ломать:** `docs/REFRAME_FPS_GRID_INVARIANT.md` (Δ=0 кадровая сетка).

---

## 1. Мотивация и задача

Продукт режет длинные ГОРИЗОНТАЛЬНЫЕ подкасты/интервью в вертикальные 9:16 клипы. Сейчас, когда в
кадре ≥2 разнесённых лица, reframe уходит в **wide** (весь кадр + блюр-рамки) — «горизонтальный» вид.
Клиенты просят **tight-наведение на говорящего** с **переключением между спикерами как жёсткий CUT
(будто вторая камера)**, а НЕ медленный пан/слайд (пан читается как «ИИ-слоп»).

**Ключевая находка ресёрча:** это ОДНА фича, а не две. Наивную «tight на говорящем» уже отгружали и
откатили (`7e7d754`), т.к. в одной непрерывной камере `_pick_target` выбирает ОДНОГО победителя на ВЕСЬ
шот → залипал tight навсегда, «никогда не wide». Причина — **не было механизма ПЕРЕКЛЮЧЕНИЯ**. Значит
переключение-как-cut — это то, что делает tight вообще пригодным на одно-камерном подкасте.

**Вторая находка:** переключение — задача слоя ПЛАНИРОВАНИЯ, не рендера. Рендер уже режет любую границу
региона жёстким `concat`-cut'ом (`stage5_render`; xfade выпилен намеренно). Значит «вторая камера» =
вставить синтетическую, frame-snapped границу региона в момент смены говорящего.

**Третья находка:** диаризация (`Word.speaker`, Deepgram `diarize=true`) УЖЕ есть в джобе, но НЕ
подключена к reframe. Это наш неиспользуемый робастный сигнал тайминга (audio-level, точно по словам).

## 2. Продуктовые решения (основатель, 2026-07-13)

1. **Default = tight speaker-follow; wide становится opt-in опцией** (флип старого «always wide на 2+»).
   ⇒ это ALL-USERS изменение дефолта, а не только opt-in → выше планка робастности/фолбэка.
2. **Фолбэк, когда чистый cut небезопасен → wide (fit).** Никакого split-screen в v1.
3. **Селектор v1 = greedy** (lookahead min-hold + hysteresis + IoU-гейт). DP (полный EditIQ energy-min)
   = P2 fast-follow на тех же сигналах.

## 3. Scope

**In (v1):** 2 спикера, одна непрерывная камера (главный кейс); tight-наведение + cut-переключение по
диаризации; wide-фолбэк; per-user/per-job тумблер (default tight); TDD на чистой логике; локальная
валидация глазами на реальном видео.

**Out (v1, → следующие фазы):** DP-селектор (P2); полный 180°/eyeline framing (P2); split-screen; ArcFace
reID-лейны (P3); замена детектора S3FD→RetinaFace (P3, там же лицензии); 3+ спикера tight (v1 → wide);
LLM-режиссёр (EditIQ Опция 4, премиум); J/L-cut audio-lead (опц. полиш).

## 4. Как reframe работает сейчас (ground truth, кратко)

`run.py:render_one_clip` → `stage3_reframe.reframe_segment` → `db.put_reframe_regions` (БД-кэш) →
`stage5_render.render_clip`.

- **Склейки:** `detect_scene_cuts` (PySceneDetect, `stage3_reframe.py:~1082`) → шоты `build_shots_frames`.
- **Лица:** `asd_reframe.score_tracks_in_segment` (`:64`) → MediaPipe BlazeFace + `stage3_speaker.build_tracks`
  (жадный IOU, `:51`). ⚠️ Треки сбрасываются на склейке (`_spans_cut`, `:40`). В одной непрерывной камере
  трек живёт весь шот (стабильные лейны).
- **ASD:** `asd/scorer.py:score_track` (Light-ASD @25fps) → **один усреднённый скаляр `SpeakerTrack.speak`
  на трек** (⚠️ см. §6.3 — для turn-level binding нужен per-frame, придётся вернуть массив).
- **Решение:** `plan_regions` (`:697`), один регион = один шот. `_is_wide_shot` (`:452`) → fit на 2+
  разнесённых лица; иначе `_pick_target` (`:516`) → макс-speak. Режим/кроп КОНСТАНТЕН внутри шота.
- **Рендер:** регионы → `trim`+`crop/fit`+`concat` = hard-cut. Кроссфейда нет.
- **Кэш:** `job_artifacts.reframe_regions` (БД) отдаётся редактору/превью без пересчёта
  (`main.py:get_clip_reframe` `:2378`, `reframe_cache.plan_to_regions` `:471`).
- **Диаризация:** `Word.speaker` (`models.py:64`); потребители — `stage2_select` (`_primary_speaker` `:152`,
  `snap_end_index` `:173`) и `editor/silence.py`. `stage3_reframe`/`asd_reframe` её НЕ видят.

## 5. Обзор дизайна

```
Реальный шот (PySceneDetect, как сейчас; в тест-видео весь клип = 1 шот)
  ├─ face-лейны на ВЕСЬ шот (существующий MediaPipe+IOU; НЕ дробим — лейн = один человек весь шот)
  ├─ [ИЗМ] Light-ASD → per-frame speak-массив на лейн (сейчас коллапсится в скаляр — вернуть массив)
  ├─ [НОВОЕ] диаризация Word.speaker → турны (реплики) по словам: build_turns()
  ├─ [НОВОЕ] binding: кластер-спикер ↔ лейн-лицо, по per-turn ASD-голосованию: bind_lanes()
  └─ [НОВОЕ] scheduler: турны + binding + IoU + min-hold → суб-регионы (fill-лейн | fit-wide): plan_switches()
       └─ границы frame-snapped (round(t*fps)/fps), ≥ min-hold, снап на онсет первого слова + паузу
  → регионы → stage5_render hard-cut (без изменений) = «вторая камера»
```

Всё новое живёт в **чистом модуле** `pipeline/speaker_switch.py` (pure-функции, TDD). `plan_regions`
получает опц. `words` + binding и, если tight-mode + 2 связанных спикера, **суб-делит реальный шот** на
регионы через scheduler; иначе — прежнее поведение байт-в-байт.

## 6. Детальный дизайн

### 6.1 Прокидка диаризации
`words: list[Word]` (с `.speaker`) прокинуть в `reframe_segment` → `plan_regions` (новый опц. параметр,
`None` → прежнее поведение). В контейнере доступно через `store.load_transcript_words` (уже грузится в
`tasks.py`/`run.py`). Клип-относительные времёна: `word.start/end − clip_source_start`.

### 6.2 Турны — `build_turns(words, t0, t1) -> list[SpeakerTurn]` (PURE)
- `SpeakerTurn = {speaker: int, t_start: float, t_end: float, word_idx: range, n_words: int}`.
- Группируем подряд идущие слова с одним `.speaker`; смежные турны одного спикера, разделённые паузой
  < `turn_merge_gap` (~0.4с) — сливаем. Слова без `.speaker` (None) наследуют текущего.
- Прайор-арт: `stage2_select._primary_speaker` / `snap_end_index` (та же логика смены спикера/паузы).

### 6.3 ⚠️ Per-frame ASD (необходимая правка scorer)
`asd/scorer.py:score_track` внутри считает per-frame speak, потом усредняет в скаляр. **Вернуть массив**
(`speak_frames: np.ndarray`) рядом со скаляром (backward-compat: скаляр = mean остаётся). `asd_reframe`
прокидывает массив в `SpeakerTrack` (новое поле `speak_frames`, ресемплится вместе с cx в `resample_track`).
Нужно для turn-level binding (один скаляр на 45-мин шот бесполезен).

### 6.4 Binding — `bind_lanes(turns, tracks) -> dict[cluster:int, lane:int|None]` (PURE)
Для каждого турна: лейн с макс. средним `speak_frames` на кадрах турна получает голос за этот кластер.
Агрегируем голоса по кластеру за весь шот → `cluster → argmax lane` (majority). Уверенность = доля голосов
за победителя; ниже `bind_conf_min` (~0.6) → `None` (кластер не привязан → off-screen/ambiguous → wide).
Робастность: одиночный турн с плохим ASD не ломает binding (majority по всему клипу).

### 6.5 Scheduler — `plan_switches(turns, binding, tracks, geom, cfg) -> list[SubRegion]` (PURE)
`SubRegion = {t0, t1, target: lane:int | WIDE}`. Greedy с lookahead:
1. **Старт:** establishing wide `establish_sec` (~0.5–1.0с; на длинном клипе можно 0) ИЛИ сразу лейн
   первого турна, если он держит ≥ min-hold.
2. Идём по турнам по времени; держим `cur_target` + `cur_start`.
3. Кандидат switch = `binding[turn.speaker]`. Если `== cur_target` → продлеваем.
4. Иначе проверяем **условия коммита** (все должны выполниться, иначе см. §6.6 фолбэк):
   - **Lookahead min-hold:** новый спикер держит слово ≥ `min_hold_sec` (`l`, старт 1.5–2.0с; сумма
     подряд идущих турнов его кластера). Иначе — НЕ переключаемся (бэкченел «ага/да» игнорируется; линжер
     на текущем = L-cut).
   - **Hysteresis:** после коммита не режем обратно раньше `min_hold_sec`.
   - **IoU-гейт (EditIQ O):** кроп кандидата vs текущий кроп, IoU в source-пикселях ≤ `iou_alpha`
     (0.15) → cut разрешён; > 0.3 → запрещён. ⚠️ **wide↔fill исключены из гейта** (легитимный
     establishing→closeup). Высокий IoU (сидят близко / почти тот же кадр) → wide-фолбэк.
5. **Снап границы:** на онсет первого слова входящего, втянутый в пред-онсетную паузу (>~0.15с гэп), и
   на нативный кадр `round(t*fps)/fps`. Закрываем `cur`, открываем новый регион.
6. **Пост:** каждый регион ≥ `min_hold_sec`; мелкие остатки поглощаем в соседа/wide (НЕ через
   `merge_short_regions` — он про анти-строб на РЕАЛЬНЫХ склейках; свой merge с учётом target).

### 6.6 Wide-фолбэк — явные триггеры
Регион = `WIDE`, если выполняется хотя бы один явный триггер: (1) 3+ привязанных спикера в пролёте (v1
tight только на 2); (2) активный кластер без привязанного видимого лейна (off-screen, `binding=None`);
(3) низкая уверенность binding; (4) IoU кандидата > `iou_beta` (слишком похожие кропы). Никакого
split-screen (out of scope v1).

**Линжер vs wide (устранение неоднозначности):** короткая реплика/бэкченел, не проходящий min-hold, →
НЕ переключаемся и НЕ уходим в wide, а ЛИНЖЕРИМ на текущем таргете (L-cut). В wide уходим ТОЛЬКО по
триггерам 1–4 выше (т.е. когда сам текущий/кандидатный таргет невалиден). Быстрое устойчивое чередование
(оба держат < min-hold попеременно) в greedy-v1 просто линжерит на последнем закоммиченном таргете — это
приемлемый L-cut; глобально-оптимальный «→ wide на весь пролёт чередования» — то, что докупает DP в P2
(EditIQ показывает, что это эмерджентно из энергии, а не отдельное правило).

### 6.7 Эмиссия регионов (интеграция в plan_regions)
Для каждого `SubRegion`:
- `WIDE` → существующая fit-ветка.
- `fill-лейн` → существующий путь: `compute_locked_cx`/`_fill_points_for_target` (`:648`) НА СУБ-ДИАПАЗОНЕ
  `[t0,t1]`, cx берём из лейна (spanning-трек, его cx на кадрах суб-региона — это ФИЧА: тот же человек весь
  клип, мы намеренно выбираем разные лейны по расписанию).
- **НЕ ре-трекаем** (не вставляем в `cuts` до трекинга — иначе `_spans_cut` раздробит лейны и убьёт
  binding). Режем ПОСТ-ФАКТУМ (шаблон — `reframe_cache._split_region_at` `:173`, но авто).
- **Границы frame-snapped** (`round(t*fps)/fps` нативный fps) — grid Δ=0.

### 6.8 «Вторая камера» — кадрирование (анти-«фотку подвинули»)
- v1: IoU-гейт (§6.5) + **лёгкое различие масштаба/хедрума** между fill-лейнами (напр. чуть разный
  `speaker_crop_scale`), чтобы общий статичный фон меньше выдавал.
- P2: полный **180°/eyeline** — A на левой трети смотрит вправо-в-кадр, B на правой смотрит влево =
  shot-reverse-shot (в тест-видео это выходит естественно: люди сидят лицом друг к другу).

### 6.9 Тумблер / данные (шаблон — `remove_pauses`, JOURNAL 2026-07-12)
- Колонка `jobs.reframe_mode TEXT DEFAULT 'speaker'` (`'speaker'` | `'wide'`), миграция + sqlite
  `_ensure_column`. **DEFAULT = 'speaker'** (tight).
- `ensure_edit`/`default_clip_edit(pref_reframe=…)` читает у владельца джобы, сидит поле в `ClipEdit`
  (напр. `reframe_mode`), прокидывается в `plan_regions`/`resolve_regions_accurate`. `'wide'` → форс fit
  (существующий `mode_setting="fit"`).
- Фронт: тумблер «Кадр: за спикером / широкий» в `SourceForm` (+ проброс через createUploadJob-цепочку) и/
  или в редакторе (per-clip). i18n en+ru в ОБА каталога (`messages/{en,ru}.json`) — иначе tsc/рантайм упадёт.
- Ручной override в редакторе — переиспользуем `op_cut`/`op_crop`/`_tight_lock_cx` (`main.py:1914+`).

## 7. Интеграция — что меняем (seams)

| Файл | Правка |
|------|--------|
| `pipeline/speaker_switch.py` (НОВЫЙ) | `build_turns`, `bind_lanes`, `plan_switches`, `SubRegion` — все PURE |
| `asd/scorer.py:score_track` | вернуть `speak_frames` (per-frame массив) рядом со скаляром |
| `pipeline/asd_reframe.py` | прокинуть `speak_frames` в `SpeakerTrack`; ресемпл в `resample_track` |
| `pipeline/stage3_reframe.py:SpeakerTrack` | новое поле `speak_frames` |
| `pipeline/stage3_reframe.py:plan_regions` | опц. `words`+`reframe_mode`; при tight+2 lane → `plan_switches` суб-деление шота; иначе прежнее |
| `pipeline/stage3_reframe.py:reframe_segment` | прокинуть `words` внутрь |
| `run.py` / `tasks.py` | прокинуть `words` (уже грузятся) + `reframe_mode` из edit в reframe_segment |
| `models.py` | `ClipEdit.reframe_mode`; (Word.speaker уже есть) |
| `config.py` | новые кнобы (§9); `reframe_wide_speak_min` реанимируется/заменяется |
| миграция + `db`/`ensure_edit`/`default_clip_edit` | `jobs.reframe_mode`, сид дефолта |
| `apps/web` (`SourceForm`, createUploadJob-цепочка, i18n en/ru) | тумблер |

**Кэш:** после смены логики планирования — `update public.job_artifacts set reframe_regions = null;`
(и чистить stale `clip_edits`) ПОСЛЕ деплоя, иначе фикс невидим на готовых клипах.

## 8. Фазировка

- **P0 (read-only, БЕЗ рендера):** прокидка диаризации + per-frame ASD + `build_turns` + `bind_lanes` +
  DEBUG-дамп «кто/когда/какое лицо/какой кроп» на тест-видео (§11). Валидируем АТРИБУЦИЮ глазами до рендера.
- **P1:** тумблер (default tight) + `plan_switches` (greedy) + суб-деление + wide-фолбэк + рендер. TDD на
  чистых функциях; grid Δ=0; глазами покадрово на стыке.
- **P2:** DP-селектор (EditIQ energy-min U/O/R/T/M) свапом `plan_switches` на тех же сигналах; eyeline-framing.
- **P3 (опц.):** ArcFace reID-лейны (окклюзия/отражения) + RetinaFace (лицензии решаем здесь).

## 9. Константы (старт, тюнить) — из EditIQ, адаптировано под short-form

| Кноб | Старт | Источник / примечание |
|------|-------|------------------------|
| `switch_min_hold_sec` (l) | 1.5–2.0с | EditIQ l=1с (кино); short-form строже — главный анти-пинг-понг рычаг |
| `switch_target_hold_sec` (m) | 3–4с | EditIQ m=7с (кино-средняя) — короче под шортсы |
| `switch_iou_alpha` | 0.15 | EditIQ α — ниже = cut разрешён |
| `switch_iou_beta` | 0.30 | EditIQ β — выше = cut запрещён (wide-фолбэк) |
| `establish_wide_sec` | 0.5–1.0 (0 на длинных) | EditIQ 2с — доля 15с-клипа велика |
| `turn_merge_gap_sec` | 0.4с | склейка смежных турнов одного спикера |
| `bind_conf_min` | 0.6 | ниже → кластер не привязан → wide |
| `onset_pause_min_sec` | 0.15с | втягивание cut'а в пред-онсетную паузу |

## 10. Тесты / DoD

- **TDD (правило CLAUDE.md — чистая логика):** `build_turns` (границы/паузы/слияние), `bind_lanes`
  (голосование, off-screen→None, шумный турн не ломает majority), `plan_switches` (min-hold отсекает
  бэкченел, hysteresis, IoU-гейт → wide, снап на кадр, фолбэк-триггеры), пограничные fps (24/25/29.97/30/60).
- **Grid Δ=0:** каждая синтетическая граница `|t0*fps − round(t0*fps)| ≈ 0` (harness как `verify_grid_fix.py`).
- **⚠️ ОБЯЗАТЕЛЬНО ГЛАЗАМИ, покадрово, на РЕАЛЬНОМ рендере на стыке** (тест-видео §11): unit-тесты и
  чистый локальный рендер НЕ ловят перцептивное качество + риск «одинаковый фон» (урок UniTalk +
  REFRAME_FPS_GRID). Проверяем: (1) наводится на ГОВОРЯЩЕГО; (2) cut попадает на смену реплики; (3) нет
  пинг-понга на быстром диалоге; (4) cut читается как вторая камера, а не «слайд/подвинули фото».
- Локально; деплой — решение основателя потом (local-first, [[local-first-then-deploy]]).

## 11. Тест-ассет (DoD)

`C:\Users\user\Downloads\Telegram Desktop\JIQwWepYgMyXUW9xg9Sw+zcLr7PvvXDY.mp4`
- 1920×1080, **30 fps ровный** (grid чистый), **45.8 мин**, стерео aac 44.1кГц.
- **Одна зафиксированная камера весь клип** (кадр идентичен на 220/824/1511/2198с) → PySceneDetect ≈ 0
  склеек → весь клип = ОДИН шот → текущий пайплайн уходит в wide всё время = точная демонстрация дыры.
- **Ровно 2 разнесённых спикера** (мужчина левая треть, женщина правая), оба с петличками (чистое аудио
  → надёжная диаризация). 9:16-кропы непересекающиеся (IoU≈0) → cut безопасен, eyeline reverse-shot
  выходит естественно. Общий статичный фон → максимально провоцирует риск «фотку подвинули» = идеальное
  место проверить IoU-гейт + eyeline.

## 12. Риски / грабли

1. **Одинаковый беспараллаксный фон** — cut может читаться как «фотку подвинули». Митигация: IoU-гейт +
   различие масштаба (v1) + eyeline (P2). В литературе для same-frame cut не решено → **глазами**.
2. **ASD-коллапс in-the-wild** (95%→76–83%, UniTalk) — binding по majority + min-hold страхуют; не
   доверять мгновенному ASD.
3. **Off-screen/кросток** — `binding=None`/оба горячи → wide, не гнаться за призраком.
4. **Grid Δ=0** — синтетическая граница = `round(t*fps)/fps` нативный; тестить на не-целом fps.
5. **Baked-cache staleness** — `reframe_regions=null` после деплоя.
6. **`merge_short_regions`** (1.5с) — свой merge для switch-регионов, не общий.
7. **Per-frame ASD** — правка scorer (§6.3) не должна замедлить/сломать существующий скаляр-путь.
8. **Лицензии весов** — v1 НЕ трогает детектор (существующий S3FD/MediaPipe/Light-ASD MIT); свап на
   RetinaFace/ArcFace = P3, там решаем (SPELL non-comm искл., InsightFace weights non-comm, ⚠️ S3FD-вес
   неизвестен — проверить).

## 13. Открытые вопросы (не блокируют v1)
- Точный J/L-cut lead для same-frame cut (в литературе нет) — A/B на P1/P2.
- Нужен ли establishing wide на длинных клипах (вероятно 0) — решаем на тест-видео.
- Порог `switch_min_hold_sec` под short-form — тюним глазами на тест-видео.

## 14. Ссылки
- Ресёрч-отчёт: `docs/archive/2026-07-13-active-speaker-reframe-research.md`
- EditIQ: arXiv 2502.02172 · инвариант: `docs/REFRAME_FPS_GRID_INVARIANT.md`
- Прайор-арт кода: `stage2_select` (диар-турны), `reframe_cache._split_region_at` (сплит-шаблон),
  `main.py:_tight_lock_cx` (ручной tight-lock)
