# Сессия 2026-07-07 — reframe-фиксы + B1 + прод-деплой + editor-export UX

Дневная фаундер-сессия. Ветка `feat/clip-quality-recall-and-editor-export`, **ОТГРУЖЕНА В ПРОД**
(merge в `cloud/main`, worker Cloud Run redeploy, Vercel автодеплой). Это «почему/как», не текущая
правда — актуальное состояние см. `docs/README.md`. Хронология — `docs/JOURNAL.md`.

## Что сделано (по коммитам ветки поверх night-run)

| Коммит | Что |
|---|---|
| `ccd53e5` | **fix: плечо на короткой перебивке.** Корень — НЕ пропущенный cut (debug-дамп треков опроверг): `merge_short_regions` сворачивал перебивку < min_hold в предыдущий fill, держа тайт-кроп говорящего поверх лица слушателя → плечо. Фикс `_crop_would_misframe` + флаг `reframe_keep_cutaways`. |
| `edcd400` | **fix: молчащий крупный план → fill, а не wide.** Silence-гейт уводил в wide ЛЮБОЕ молчащее лицо; теперь только МЕЛКОЕ (`_SILENCE_MIN_FILL_FACE=0.12`) — крупный план наводится на лицо, молчит или нет. Заменяет Tom-Holland-урок «молчит→wide». |
| `5808b78` | **fix: перебивка держит СВОЙ кроп.** Порог промаха `_crop_would_misframe` 0.5→0.33 кропа: короткая перебивка другого человека (clip_06 0.59 vs 0.44, Δ=0.15) больше не срезается держащимся чужим кропом. |
| `5e8a34d` | **fix: плавная наводка (follower).** Мелкая коррекция шла рывками — кейфреймы эмитились раз в 0.5с + min_delta 0.012, а рендер линейно интерполирует → скачки скорости. `_FOLLOW_SAMPLE_SEC 0.5→0.15` + `min_delta 0.012→0.003`. (реализация — саб-агент/fork; ревью+гейт+коммит — оркестратор.) |
| `30c5db2` | **feat: B1 vision ВКЛ.** `vision_review_enabled=True` (ре-ранк моментов по мультимодальной виральности). Новый `vision_review_trim_edges=False`: B1-edge-refine снапил к ±1 ЛЮБОГО слова (не .?!) → мог увести чистую границу на запятую; при OFF B1 ТОЛЬКО ре-ранкует, края детерминистические (10/10 клипов кончаются на `.?!`, проверено). Layer A/B2 остаются OFF (reframe детерминистический). |

## Ключевые уроки (не повторять)

1. **Reframe-баги: смотри ДАННЫЕ до фикса.** Гипотеза «плечо = пропущенный cut» была НЕВЕРНА — debug-дамп
   треков/склеек показал, что PySceneDetect склейку ЛОВИТ, виноват merge. Я построил целый `face_jump_cuts`
   под ложную гипотезу и удалил его. Систематик-дебаггинг: reproduce → **evidence** → fix.
2. **⚠️ STALE-EDIT КЭШ (стоил ложного показа фаундеру).** `render_edit_to_file` рендерит из `clip_edits`
   (таблица в `tmp/jobs.db`), НЕ из свежего `reframe_clip_XX.json`. `ensure_edit` возвращает существующий
   edit как есть → ре-ран того же job_id рендерит СТАРЫЙ интервал + старые регионы. Локальный аналог прод
   двухслойного кэша (`job_artifacts.reframe_regions`). Перед локальной проверкой: `DELETE FROM clip_edits
   WHERE job_id=?`. В проде: `update public.job_artifacts set reframe_regions = null;` после деплоя reframe.
   Надёжная истина — детерминистический `dbg_clip` (plan_regions на фикс-интервале) + ручные ffmpeg-кропы.
3. **B1/B2 вердикт.** B1 помогает ВЫБОРУ моментов (ре-ранк по виральности — подтверждено). Для REFRAME его
   per-shot разметка (Layer A) слишком груба (молчащий интервьюер оставался wide даже с Layer A ON) → reframe
   чиним детерминистикой. B2 (subject tie-break) — маргинально, OFF.

## OOM-инцидент (прод)

Джоб с 22 клипами падал `container fan-out: no result (clip execution crashed/killed)`. Логи: `Container
terminated on signal 9` = SIGKILL по памяти. ФС Cloud Run — tmpfs (в RAM): каждый `quip-render` качает полный
исходник в память + кадры reframe → на длинном видео > 16Gi → OOM. Быстрый фикс: `quip-render` память
**16→32Gi** (config-апдейт, без пересборки). Правильный фикс (стримить/не держать исходник целиком в RAM) —
следующей сессией.

## Метрики (по длине входа)

Потолок одного видео `MAX_VIDEO_MINUTES=180` (3ч). Биллинг в МИНУТАХ (пул: free 120 / starter 600 / pro 1800
/ studio 2520 мин/мес). 90-мин видео → ~10-12 клипов, ~$0.48 API (Deepgram $0.39 + chunked-select $0.09,
замерено; B1 +$0.015), полная себест. ~$1.0-1.2. Локально 10 клипов ~11.7 мин (serial, узкое место — ASD
78-228с/клип); прод 6 клипов параллельно.

## Editor-export

Фича (EDL/FCPXML/markers/VTT/SRT/package.zip) — бэкенд `app/editor/interchange.py` + эндпоинты в `main.py`
(`/export.edl`, `.fcpxml`, `/markers.csv`, `/clips/{id}/export.vtt`, `/export/package.zip`); фронт
`components/ExportMenu.tsx` (в ClipCard + EditorHeader). Не работала до сегодня т.к. эндпоинтов не было в проде
(404). Все 6 форматов провалидированы (`_build_package_zip` на реальном джобе — EDL drop-frame CMX3600, FCPXML
валидный XML, CSV, WebVTT, SubRip). Меню переделано **format-first → editor-first**: «Всё (.zip) / Premiere Pro
(.edl) / DaVinci Resolve · Final Cut (.fcpxml)» (i18n en+ru); сырые markers/vtt-пункты убраны (они в zip).

## Открыто на след. сессию

- OOM: правильный фикс (стриминг исходника вместо загрузки в RAM) вместо band-aid 32Gi.
- Reframe follower — «ease всё ещё слегка резкий» закрыт (5e8a34d); остаточных reframe-задач нет.
- B2 / Layer A — прототипы, можно удалить (детерминистика их кейсы покрывает).
