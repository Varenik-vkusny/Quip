# Удаление пауз (Pause Removal) — Design Spec

**Дата:** 2026-07-12 · **Ветка:** `feat/pause-removal` · **Статус:** ✅ **Phase 1a РЕАЛИЗОВАНА**
(per-clip, backend+frontend, гейт зелёный, доказано локально); Phase 1b (глобально на загрузке) и
Phase 2 (клиентский preview) — НЕ сделаны. На утреннее ревью основателя.

> **Что сделано (ветка `feat/pause-removal`):**
> - `be7058d` — ядро `editor/silence.py` (`compute_silence_cuts`/`apply_silence`, TDD 12 тестов) +
>   `ClipEdit.silence_config` (codegen) + интеграция в `render_edit_to_file` (подмена
>   `source_intervals` через `apply_silence` → captions/reframe/render_timeline авто-сжимаются) +
>   эндпоинт `POST /clips/{id}/edit/silence`.
> - `f00e569` — вкладка «Паузы» (`PausesTab.tsx`) в редакторе: тумблер + слайдер плотности +
>   advanced (max_pause/padding) + `setClipSilence` API + i18n en/ru.
> - **Доказано локально:** клип 60.0с → 57.7с (7 frame-snapped интервалов, стыки чистые — нет флеша,
>   субтитры выровнены). Плотная речь Маргулана → мало гэпов (−2.27с); на контенте с паузами больше.
> **НЕ сделано (следующие шаги):** Phase 1b — глобальный тумблер на загрузке (нужен `apply_silence` в
> `run.py:render_one_clip` + проброс `remove_pauses` job-параметра; клип-эдит создаётся ЛЕНИВО, поэтому
> первичный рендер идёт мимо него — это отдельная поверхность). Phase 2 — клиентский live-skip превью.

> Заменяет/уточняет наброски Phase-2 из `2026-06-28-silence-removal-design.md` (тот предшествовал
> NLE-редактору и боялся reframe-ремаппинга как «hard part» — теперь это решено движком).

---

## 1. Суть фичи

Вырезать из клипа **паузы** (тихие промежутки, где никто не говорит), сжимая клип в плотную версию.
Два уровня (как просил основатель):
1. **Глобально на загрузке** — тумблер «убрать паузы» → ВСЕ клипы джобы выходят плотными.
2. **Per-clip в редакторе** — вкл/выкл на конкретном клипе; выкл = вернуть оригинал с паузами
   («наоборот добавить паузы» = восстановить исходные, недеструктивно); + слайдер плотности.

Бонус-мотивация (из сессии отбора): это даёт виральную компрессию — длинная арка ~85с сжимается в
~50с (человек в виральнике делал это jump-катами вручную).

**Scope = ТОЛЬКО тишина** (гэпы между словами). Удаление слов-филлеров («эээ», «ну» посреди речи) —
ОТДЕЛЬНАЯ будущая фича (режет речь, нужен другой UX/риск). Здесь НЕ трогаем.

---

## 2. Архитектурный контекст (почему это низкий риск)

Ресёрч кода main (HEAD `4a29612`) показал: **рендер УЖЕ поддерживает jump-cut'ы нативно.**

- `ClipEdit.source_intervals: list[SourceInterval]` (`models.py:190,328`) — таймлайн клипа = **список
  интервалов**, не один [start,end]. Порядок = clip-порядок.
- `render_timeline` (`stage5_render.py:790-848`) при `len(intervals)>1` → `flatten_timeline` +
  `build_timeline_filter` конкатят интервалы с жёсткими cut'ами (`concat`), выкидывая время между ними.
- Редактор УЖЕ пробивает внутренние дырки: `ops._subtract_range` (`editor/ops.py:19-30`), исп. в
  `apply_trim` — разрезает интервал, удаляя середину.
- **Субтитры авто-ремапятся** на сжатый таймлайн: `captions_v2.py:592-600` через
  `ClipTimeMap.source_to_clip` (`timemap.py:32-37`) — слово в дырке → `None` → реплика дропается.
- **Reframe-регионы ремапятся** per-interval через `band_starts` (`regions_to_clip_time`), frame-snap
  каждой границы на native-fps грид (`flatten_timeline` / `apply_cuts_to_regions`).

**Вывод:** удаление пауз = вычислить тишину-интервалы → вычесть их из `source_intervals` → отдать
рендеру. Всё downstream (concat/subs/reframe) уже умеет мульти-интервал. **Нового рендер-движка НЕ надо.**

**Единственный реальный watch-out — reframe plan fast-path.** `plan_to_regions` возвращает `None`
если `len(intervals)!=1` (`reframe_cache.py:442-445`) → мульти-интервальный (пауза-резаный) клип
обходит быстрый путь и честно пересчитывает CV (`resolve_regions_accurate`). Это КОРРЕКТНО (геометрия
верна, frame-snap соблюдён), но МЕДЛЕННЕЕ. Latency-оптимизация (расширить авторитет плана на дырки) —
отложена.

---

## 3. Решения (принято автономно; альтернативы для ревью)

| # | Решение | Выбор | Обоснование / альтернатива |
|---|---------|-------|----------------------------|
| D1 | Источник пауз | **Гэпы между словами** (транскрипт, абс. source-сек) | Детерминированно, данные уже есть (`getClipAnalysis().words`). _Альт:_ VAD/loudness для лидинг/трейлинг тишины — отложено (края уже триммит boundary-логика Машины B) |
| D2 | Что резать | **Двусторонний порог**: режем гэп ∈ [`min_pause`, `max_pause`]; НЕ трогаем <min (естеств. ритм) и >max (драматич. пауза) | Сохраняет живость речи и драматургию. Дефолты min=400мс, max=2000мс, padding=50мс |
| D3 | Модель хранения | **Отдельный слой `silence_config` на `ClipEdit`**, применяется в РЕНДЕРЕ (не персистим вычтенные интервалы) | Чистый toggle (флип enabled) + чистая композиция с ручными правками. _Альт:_ запекать прямо в `source_intervals` — ломает toggle/undo и мешается с ручным тримом. ОТВЕРГНУТО |
| D4 | Применение | **Общий helper** `apply_silence(intervals, words, cfg)` в рендер-пути; вызывают ОБА входа: `render_edit_to_file` (per-clip) и `render_one_clip` (глобально на загрузке) | Одна чистая функция, оба пути. Frame-snap границ обязателен |
| D5 | Toggle off / «вернуть паузы» | `enabled=false` → рендер использует оригинальные `source_intervals` | Недеструктивно; исходник всегда цел |
| D6 | Композиция с ручными cut'ами | Тишина-дырки вычитаются из `source_intervals` ПОСЛЕ ручных правок; shot split/merge ортогонален (меняет группировку кропа, не время) | Естественно композится (оба — hole-punch/regroup) |
| D7 | Reframe/subs | Наследуем из движка (мульти-интервал уже ремапится); при пауза-резе — CV-пересчёт reframe | Корректно; latency-оптимизация отложена |
| D8 | Клиентский live-preview | **Отложен в Phase 2** | Рендер WYSIWYG + `pixelDirty`→ре-рендер как у trim/crop. v1 = «поменял конфиг → рендер → видишь плотный». Live-skip (rVFC) — полиш |
| D9 | Дефолт | **OFF** (opt-in) | Меняет ощущение клипа; пусть включают осознанно |
| D10 | Гейт по плану | НЕ гейтим в v1 (флаг для будущего) | Флаг основателю на ревью: делать ли премиум-фичей |

---

## 4. Сценарии (проверка «без хуйни»)

- **S1 Глобально ON на загрузке** → `remove_pauses` job-param → `default_clip_edit` ставит
  `silence_config.enabled=true` → первичный рендер (`render_one_clip`) печёт плотные клипы.
- **S2 Глобально OFF (дефолт)** → клипы с паузами; юзер включает per-clip.
- **S3 Per-clip ON→OFF** → флип конфига → ре-рендер полного `source_intervals` (паузы вернулись).
- **S4 Плотность** → слайдер меняет `min_pause` → пересчёт дырок → ре-рендер.
- **S5 Композиция с ручными shot-cut'ами** → тишина режет ВРЕМЯ из `source_intervals`; shot split/merge
  регруппирует КРОП — ортогонально, оба идут в `flatten_timeline`.
- **S6 Reframe+subs на сжатом таймлайне** → уже ремапится движком (band_starts / source_to_clip).
- **S7 Скачивание** → silence меняет геометрию → `pixelDirty` → `ConfirmRenderModal` → ре-рендер →
  плотный mp4. **Cache-key ОБЯЗАН включать silence-состояние** (иначе stale bakedUrl — постмортем Octarin).
- **S8 Клип без слов / все гэпы < min** → нет дырок → tight == оригинал (no-op).

---

## 5. Ядро — вычисление тишина-дырок (pure, TDD)

Новый модуль `services/worker/app/editor/silence.py` — ТОЛЬКО чистые функции, unit-тесты первыми.
`SilenceConfig` (pydantic-модель) живёт в `models.py` (§6 — нужен codegen в `packages/shared`);
`silence.py` её импортирует. Дефолты поля: `enabled=False`, `min_pause_ms=400` (короче — ритм, не
режем), `max_pause_ms=2000` (длиннее — драма, не режем), `padding_ms=50` (по краям речи).

```python
def compute_silence_cuts(
    words: list[Word], intervals: list[SourceInterval], cfg: SilenceConfig, *, fps: float
) -> list[tuple[float, float]]:
    """Вернуть source-интервалы (сек) ТИШИНЫ к вырезанию. Границы FRAME-SNAPPED на fps-грид
    (инвариант REFRAME_FPS). Гэп режется если min_pause <= gap <= max_pause И он ВНУТРИ kept-интервала.
    padding сохраняется по краям (не режем впритык к согласной)."""

def apply_silence(
    intervals: list[SourceInterval], words: list[Word], cfg: SilenceConfig, *, fps: float
) -> list[SourceInterval]:
    """intervals − silence_cuts. Reuse ops._subtract_range на каждую дырку. Дропает вырожденные
    (< ~1 кадр) интервалы. cfg.enabled=False → intervals без изменений (строгий no-op)."""
```

**Инварианты (тесты):**
- `enabled=False` → выход == вход (байт-в-байт).
- Гэп < min_pause → НЕ режется (ритм). Гэп > max_pause → НЕ режется (драма).
- Гэп ∈ [min,max] → режется, с padding по краям.
- Границы дырок кратны 1/fps (frame-snap) — как ручные cut'ы (Δ=0).
- Нет слов → нет дырок.
- padding не создаёт инвертированных/нулевых интервалов (кламп).
- Композиция: работает на мульти-интервальном входе (после ручного трима).
- Overlapping ASR-тайминги (gap<0) → не режем.

**Frame-snap:** переиспользовать snapping ручных cut'ов (Δ=0-логика из `apply_cuts_to_regions`/
`reframe_cache`); границу дырки округлять к ближайшему кадру ТАК ЖЕ, иначе вернём ±1-кадровый флеш.

---

## 6. Данные — `SilenceConfig` на `ClipEdit`

`services/worker/app/models.py`:
- Добавить модель `SilenceConfig` (§5).
- В `ClipEdit` (models.py:328): `silence_config: SilenceConfig | None = None`.
- `default_clip_edit(...)` принимает опц. `silence: SilenceConfig | None` (для глобального job-параметра).
- После правки `models.py` → `just types` (codegen `packages/shared`; TS-тип `SilenceConfig` +
  `ClipEdit.silence_config`). Руками `packages/shared` НЕ трогать (anti-drift гейт).

`version`-бамп при смене конфига (optimistic-lock как у прочих правок).

---

## 7. Интеграция в рендер (общий helper, оба входа)

- **Per-clip** — `tasks.py:render_edit_to_file` (полный путь, ~332): перед `render_timeline` вычислить
  `effective = apply_silence(edit.source_intervals, words, edit.silence_config, fps=src_fps)` и рендерить
  `effective`. `words` берём из транскрипта клипа (как в captions). Fast-path (`edit_matches_baked`) при
  `silence_config.enabled` НЕ применим (геометрия/тайминг меняются) → полный путь (и так уже так на
  trim-правках).
- **Глобально** — `run.py:render_one_clip` (первичный рендер): если у сегмента/эдита
  `silence.enabled`, построить `effective` тем же helper'ом и рендерить мульти-интервал через
  `render_timeline` (сейчас первичный клип = один интервал → станет N).
- Captions ASS строятся на `effective` (уже умеют — `captions_v2` берёт intervals). Reframe regions —
  через существующий per-interval ремап (CV-путь при мульти-интервале).

**Watch-out (латентность):** пауза-резаный клип → CV-пересчёт reframe (обход plan fast-path). Для
глобального ON это удлиняет первичную обработку ВСЕХ клипов. Приемлемо для v1; оптимизация (расширить
`intervals_match_default` на дырки, чтобы план оставался авторитетным) — отдельная задача.

---

## 8. API — установить silence-конфиг per-clip

`patchClipEdit` (клиент) шлёт ТОЛЬКО captions → нужен **отдельный эндпоинт** (не расширять общий):
```
POST /jobs/{jobId}/clips/{clipId}/edit/silence
Body: { version: int, silence_config: SilenceConfig }
→ 200 { version } (optimistic-lock; 409 EditConflict как у прочих)
```
Ставит `edit.silence_config`, бампает version, помечает клип «нужен ре-рендер» (как trim). Реализация —
рядом с `edit/cut`, `edit/set-interval` (`main.py`/роутер редактора; `store.save_edit`).

---

## 9. Глобальный тумблер на загрузке

- `CreateJobInput` (`api.ts:204`) + `createJob`/`createUploadJob` body: `remove_pauses?: boolean`
  (+ опц. `pause_tightness_ms?`).
- Воркер: job-параметр → при построении дефолтного эдита каждого клипа
  `default_clip_edit(..., silence=SilenceConfig(enabled=True, min_pause_ms=...))`.
- `SourceForm.tsx` — чекбокс «Убрать паузы» рядом с max-clips/длиной; проброс через
  `onSubmit`/`onSubmitFile` в `dashboard/page.tsx`.

---

## 10. Редактор — вкладка «Паузы»

- `EditorRail.tsx` TABS (сейчас `subtitles/hook/frame/agent`) → добавить `{ id: "pauses", icon: Scissors }`,
  расширить `Tab`-union, i18n-ключ `editor.tabs.pauses` (en+ru).
- Новый `apps/web/components/editor/PausesTab.tsx`: тумблер + слайдер плотности (Natural↔Tight, маппинг
  min_pause 800→200 из прошлой спеки) + строка «−Xс, плотный клип Yс». Advanced (padding, max_pause) —
  опц.
- Границы клипа: `edit.source_intervals[0].source_start` / `[last].source_end` (НЕ `edit.start/end`).
- onChange → новый `setClipSilence(jobId, clipId, version, cfg)` в `lib/api.ts` (§8 эндпоинт) через
  очередь мутаций (`patchChain`), ставит `pixelDirty` (→ ре-рендер-гейт при скачивании).
- Клиентская оценка «−Xс» — чистый JS-порт `computeKeepIntervals` в `apps/web/lib/silenceMap.ts`
  (только для СТАТ/оценки в v1; live-skip плеера — Phase 2). Тесты паритета с Python-логикой.

---

## 11. Фазы

- **Phase 1a (ядро, доказать механизм):** `silence.py` (pure+TDD) → `SilenceConfig` в models+codegen →
  `apply_silence` в `render_edit_to_file` → эндпоинт §8 → `PausesTab` (toggle+слайдер) →
  локальный рендер: клип ДО/ПОСЛЕ, проверить reframe+subs+frame-snap глазами покадрово на стыках.
- **Phase 1b (глобально):** `remove_pauses` job-param → `render_one_clip` + `SourceForm` тумблер.
- **Phase 2 (полиш, отложено):** клиентский live-skip в `PreviewPlayer` (rVFC), VAD для краёв,
  reframe-plan авторитет на дырках (latency), `/account` глобальный дефолт.

---

## 12. Файлы

**Воркер (Phase 1a/1b):**
| Файл | Что |
|------|-----|
| `services/worker/app/editor/silence.py` | **НОВЫЙ** — `compute_silence_cuts`/`apply_silence` (pure+TDD); `SilenceConfig` — в `models.py` |
| `services/worker/app/models.py` | `SilenceConfig` + `ClipEdit.silence_config`; `default_clip_edit(silence=)` |
| `services/worker/app/tasks.py` | `render_edit_to_file`: `apply_silence` перед `render_timeline` |
| `services/worker/app/run.py` | `render_one_clip`: применить silence при глоб. `remove_pauses` |
| `services/worker/app/main.py` (роутер) | `POST /clips/{id}/edit/silence`; job-param `remove_pauses` в `POST /jobs` |
| `services/worker/app/export_schema.py`→`just types` | codegen контракта |
| `services/worker/tests/unit/test_silence.py` | **НОВЫЙ** — TDD ядра |

**Фронт:**
| Файл | Что |
|------|-----|
| `apps/web/components/editor/EditorRail.tsx` | таб `pauses` |
| `apps/web/components/editor/PausesTab.tsx` | **НОВЫЙ** UI |
| `apps/web/components/editor/ClipEditorScreen.tsx` | state + рендер таба + `pixelDirty` |
| `apps/web/lib/api.ts` | `setClipSilence`; `remove_pauses` в `CreateJobInput` |
| `apps/web/lib/silenceMap.ts` (+тест) | **НОВЫЙ** — JS-оценка «−Xс» (паритет с Python) |
| `apps/web/components/SourceForm.tsx` + `dashboard/page.tsx` | тумблер на загрузке |
| i18n `messages/{en,ru}.json` | `editor.tabs.pauses` + строки таба |

---

## 13. Риски / watch-outs

1. **FPS-грид (REFRAME_FPS_INVARIANT):** границы тишина-дырок ОБЯЗАНЫ frame-snap'иться как ручные
   cut'ы (Δ=0), иначе ±1-кадровый флеш на стыке. Проверять ГЛАЗАМИ покадрово на реальном рендере.
2. **Reframe CV-пересчёт** на мульти-интервале (обход plan fast-path) → медленнее; для глоб. ON —
   ощутимо. Приемлемо v1, оптимизация отложена.
3. **Cache-bust скачивания:** silence-состояние в cache-key/`dirty` (иначе stale bakedUrl —
   постмортем Octarin по crop-инвалидации).
4. **Много мелких гэпов** → много concat-сегментов в одном filtergraph → проверить стоимость ffmpeg;
   при нужде — минимальная длина сегмента / лимит числа cut'ов.
5. **Аудио на стыках:** concat filtergraph даёт бесшовное аудио (asetpts) — жёсткий cut, без кроссфейда
   (jump-cut эстетика). Проверить на слух.
6. **Паритет клиент/сервер** оценки: JS и Python `computeKeepIntervals` должны совпадать (тест).

---

## 14. Локальный тест-план

1. `test_silence.py` зелёный (двусторонний порог, frame-snap, no-op при disabled, мульти-интервал,
   overlapping-тайминги, padding-кламп).
2. `just check` зелёный (ruff+mypy+tsc+eslint+anti-drift).
3. **Локальный рендер ДО/ПОСЛЕ** на реальном RU-подкасте (кэш `data/`): один клип с паузами → включить
   silence → ре-рендер → сравнить длину, **покадрово стыки** (нет флеша), субтитры не разъехались,
   reframe следует контенту, аудио бесшовно.
4. Toggle off → длина/паузы вернулись (недеструктивно).
5. Композиция: ручной trim + silence → корректный сжатый клип.
6. Показ клипов ДО/ПОСЛЕ основателю → его «ок» → потом деплой.

---

## 15. Вне scope (явно)

- Удаление слов-филлеров («эээ/ну» посреди речи) — отдельная фича (режет речь).
- Клиентский live-skip плеера — Phase 2.
- VAD/loudness для лидинг/трейлинг тишины — Phase 2 (края уже триммит boundary-логика).
- Reframe-plan авторитет на мульти-интервале (latency) — отдельная оптимизация.
- Вставка НОВЫХ искусственных пауз (не восстановление) — не требуется.
