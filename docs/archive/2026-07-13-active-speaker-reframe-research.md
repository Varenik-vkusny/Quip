# 2026-07-13 — Ресёрч: active-speaker reframe + «виртуальная вторая камера» (cut-not-pan)

> Исследовательский отчёт (НЕ текущая правда — это «почему/что можно»). Триггер: клиенты просят,
> чтобы в много-людном кадре был НЕ wide, а **tight с наведением на говорящего**; и отдельно —
> чтобы в **одной статичной камере** (подкаст, нет склеек) фокус **ПЕРЕПРЫГИВАЛ** между спикерами
> «как будто вторая камера», а не медленно СЛАЙДИЛ (панорама = «ИИ-слоп»).
> Метод: код-аудит (sub-agent) + жёсткий OSS-ресёрч (workflow, 6 углов × sweep→adversarial-verify→
> synthesize, 13 агентов, ~1M токенов). Ключевой источник-прототип: **EditIQ (ACM IUI 2025)**.

---

## 0. TL;DR / рекомендация

- **(A) и (B) — это ОДНА фича, а не две.** Наивную «tight на говорящем в 2-shot» уже отгружали и
  **откатили** (`7e7d754`): в одной непрерывной камере `_pick_target` выбирает ОДНОГО победителя на
  ВЕСЬ шот → залипал tight на одном человеке навсегда, «никогда не wide». Причина отката — **не было
  механизма ПЕРЕКЛЮЧЕНИЯ**. Значит (B) (переключение-как-cut) — это то, что делает (A) вообще
  пригодной. Порядок «сначала tight, потом переключение» — тупик; строим сразу переключение.
- **(B) — это задача СЛОЯ ПЛАНИРОВАНИЯ, не рендера.** Рендер УЖЕ режет любую границу региона жёстким
  `concat`-cut'ом (xfade выпилен намеренно). Значит «вторая камера» = **вставить синтетическую,
  frame-snapped склейку в `cuts` в момент смены говорящего** — и вся существующая машинерия выдаёт
  прыжок кропа «бесплатно».
- **Не меняем ASD-модель.** Light-ASD (наш) = 94.5% mAP, ~1M параметров, лидер по кросс-датасет
  робастности, MIT. SOTA (LoCoNet/D²Stream) даёт +~1 mAP за 10–40× компьют. Но **UniTalk 2025:** ВСЕ
  ASD-модели падают до 76–83% mAP на грязном реале → **никакая модель не спасает; спасает FUSION**
  (диаризация ⊕ ASD ⊕ lip-motion + гистерезис), а не свап модели.
- **Рекомендуемый bet:** порт **EditIQ** (глобальный DP-селектор шотов) поверх наших готовых кропов +
  **жёсткий IoU/geometry-гейт** (Опция 3), приводимый **fused-таблицей `диар-кластер ↔ face-lane`**, а
  НЕ сырым LR-ASD. Тайминг cut'а — на **онсет первого слова** нового спикера, снапнутый в пред-онсетную
  паузу (данные у нас есть). Фолбэк на wide/split при off-screen / кросстоке / слишком похожих кропах.
- **Обязательный per-user тумблер** (основатель хочет wide по умолчанию; tight — opt-in клиентам). Как
  `remove_pauses`: колонка в `jobs` + сид в `ensure_edit`.
- **Проверять ТОЛЬКО глазами, покадрово, на РЕАЛЬНОМ рендере на стыке** — unit-тесты и чистый локальный
  рендер врут (UniTalk-коллапс + риск «одинакового фона», см. §9).

---

## 1. Как reframe работает СЕЙЧАС (ground truth из кода)

Путь: `run.py:render_one_clip` → `stage3_reframe.reframe_segment` → `db.put_reframe_regions` (БД-кэш) →
`stage5_render.render_clip`.

**Сигналы на шот** (`stage3_reframe.py`):
- **Склейки** — `detect_scene_cuts` (PySceneDetect ContentDetector, thr=27) на пересжатом в НАТИВНЫЙ
  fps сегменте, снап на реальную склейку `_refine_cut_native`.
- **Шоты** — `build_shots_frames(cuts)` → `(f0,f1)` в нативных кадрах.
- **Face-треки** — `asd_reframe.score_tracks_in_segment` → MediaPipe BlazeFace (детект) →
  `stage3_speaker.build_tracks` (**жадный IOU**, thr 0.5, gap-толеранс 10 кадров, линейная
  интерполяция bbox). ⚠️ **Идентичность стабильна ТОЛЬКО внутри шота** — `_spans_cut` запрещает треку
  пересекать склейку (трекинг сбрасывается на каждой склейке).
- **ASD speak-score** — `asd/scorer.py:score_track` (Light-ASD @ **25fps**, аудио 100Гц, 4:1),
  усреднён по окнам → один скаляр `SpeakerTrack.speak` на трек. Скорится только при **≥2 треках**
  (`should_score_asd`; 0–1 трек → скип, sentinel `_SILENT=-9`).

**Решение (`plan_regions`, один регион = один шот):**
1. `_is_wide_shot` (`:452`): ≥2 стабильных (coverage ≥25% + width ≥0.08) лица, разнесённых по X >
   `spread_min` (= `crop_w_frac * _WIDE_SPREAD_RATIO(0.55)`) → **`fit`** (весь кадр + блюр-рамки).
2. Иначе `_pick_target` (`:516`): **макс. `speak`** среди кандидатов; ниже порога (0.0) → **largest-face**.
3. Silence-gейт: одиночное молчащее МЕЛКОЕ лицо (`speak<-0.6 & width<0.12`) → `fit`.
4. Кроп: `compute_locked_cx` (статичный cx на шот) или follower-траектория.

**Режим/кроп КОНСТАНТЕН внутри шота; меняется ТОЛЬКО на реальной склейке.** ← ядро проблемы: в
одно-камерном подкасте весь клип = ОДИН шот → одно наведение на весь клип.

**Рендер (`stage5_render`):** `[0:v]split` → на регион `trim=start_frame=round(t0*fps):end_frame=…` +
crop/fit → `concat` = **жёсткий cut**. Кроссфейда НЕТ (`_chain_video_segs` docstring: «xfade удалён —
сам читался как зум-вспышка»). ⇒ **любая граница региона уже рендерится как мгновенный прыжок кропа.**

**Диаризация (`Word.speaker`, Deepgram `diarize=true`) СУЩЕСТВУЕТ в джобе, но НЕ подключена к reframe
вообще.** Её потребители сейчас: `stage2_select` (`_primary_speaker`, `snap_end_index` — снап границ
клипа на смену спикера/паузу) и `editor/silence.py` (гейт удаления пауз). `stage3_reframe`/`asd_reframe`
не импортируют `Word`. ← **это наш неиспользуемый робастный сигнал для (B).**

**Кэш (два слоя):** `job_artifacts.reframe_regions` (БД) — запечённый план, отдаётся редактору/превью
БЕЗ пересчёта (`get_clip_reframe` fast-path, `plan_to_regions`). ⚠️ Смена логики планирования НЕВИДИМА
на готовых клипах, пока не `update public.job_artifacts set reframe_regions = null;` после деплоя.

---

## 2. Переосмысление задачи (главная находка)

Комментарий `stage3_reframe.py:776-781` — прямая история продукта:
> *founder: «the AI must go wide when it sees more than one person». …reverts the `7e7d754`
> "clear-speaker → crop tight on dialogue" hybrid, which kept every 2-shot cropped because in a podcast
> someone is almost always speaking (wide_speak_min=0.3).*

Т.е. гибрид «кропь tight на явного говорящего» откатили НЕ потому что tight плох, а потому что он
**залипал** (кто-то всегда говорит → всегда tight, никогда wide) и **не умел переключаться**. Кноб
`reframe_wide_speak_min=0.3` с тех пор **мёртвый** (в wide-ветке не используется).

Вывод: клиентский запрос = «tight-follow с ПРАВИЛЬНЫМ переключением». Это ре-активация гибрида, НО с
механизмом смены спикера-как-cut. Без (B) фича = ровно тот баг, что откатили.

---

## 3. OSS-ландшафт (6 углов ресёрча, fact-checked)

### 3.1 ASD-модели — **вывод: оставить Light-ASD**
- **Light-ASD / LR-ASD** (наш) — CVPR'23→IJCV'25, ~1M пар., sub-1 GFLOP, ~94.5% mAP, **MIT**
  (verifier: наша прошлая «уточнить лицензию» — снята, лицензия чистая-коммерческая), лидер
  кросс-датасет робастности, ~реалтайм на CPU. Скорит КАЖДЫЙ трек НЕЗАВИСИМО → «кто говорит» =
  argmax+threshold. Слабость: путает БЛИЗКИЕ side-by-side лица. https://github.com/Junhua-Liao/LR-ASD
- **LoCoNet** (CVPR'24, 95.2%) — единственный осмысленный апгрейд-вектор: **joint inter-speaker**
  моделирование (лучше на side-by-side), но ~34M пар., GPU. https://github.com/SJTUwxz/LoCoNet_ASD
- **TalkNCE** (ICASSP'24) — training-only loss, **0 стоимости на инференсе**; бустит любой backbone
  если когда-нибудь дообучаем на своих подкастах. https://github.com/kaistmm/TalkNCE
- **TS-TalkNet** (INTERSPEECH'23, arXiv 2305.12831) — ASD, обусловленный **эмбеддингом голоса**
  (enrollment) → «говорит ли ЭТОТ известный голос на экране» → прямой фикс profile/occluded-mouth
  случая; **мост диаризация↔лицо**. https://github.com/Jiang-Yidi/TS-TalkNet
- ⚠️ **SPELL / GraVi-T (ECCV'22) — NON-COMMERCIAL, исключить.** (verifier REFUTED «Apache».)
- **UniTalk 2025** — все модели падают 95%→76–83% mAP in-the-wild (профили, толпа, дубляж, кроссток).
  ⇒ **бигтех-модель не спасает; спасает fusion + гистерезис.**
- Дешёвые фолбэки: SyncNet (AV-sync confidence), **MediaPipe FaceLandmarker blendshapes** (jawOpen /
  lip-distance variance, Apache-2.0, CPU-реалтайм) как lip-motion tie-break.

### 3.2 Face detection + tracking + reID — **рецепт: детектор → трекер → ArcFace-binding**
Наш вендоренный стек (S3FD + чистый IOU-трекинг в пределах шота) = классика, но IOU рвёт трек на любой
окклюзии/повороте и плодит low-coverage спурьез-треки (отражения). **Фикс архитектурный (добавить
identity-слой), НЕ снижением порогов.**
- **Детекторы:** RetinaFace через **serengil/retinaface** или **biubug6/Pytorch_Retinaface** (оба
  **MIT** weights, чисто для коммерции; сильны на профиль/мелкие) — рекомендую default.
  **YOLO-FaceV2** (SEAM-attention окклюзия + **Repulsion Loss** раздвигает соседние боксы — идеально
  под 2 близких лица), но ⚠️ **AGPL-3.0** (как Ultralytics). MediaPipe BlazeFace (Apache-2.0, 200–1000
  FPS CPU) — фронтально-смещён, только secondary. ⚠️ **InsightFace SCRFD/buffalo weights =
  NON-COMMERCIAL**; и «SCRFD 5мс на CPU» — **REFUTED** (это GPU-числа; 10GF ≈ >100мс/кадр на CPU).
- **Трекеры (motion-only, MIT, CPU):** **ByteTrack** (2-й low-conf проход держит трек сквозь короткую
  окклюзию) https://github.com/ifzhang/ByteTrack; **OC-SORT** (CVPR'23, лучше re-attach). BoT-SORT'а
  CMC бесполезна на статичной камере → лучше **position-anchored «места» (seats)**.
- **Identity binding (самый высокий рычаг):** **ArcFace/InsightFace** 512-d эмбеддинг → кластеризовать
  треклеты в K локнутых «дорожек»; сразу (a) переживает окклюзию/поворот, (b) разводит соседей,
  (c) выкидывает отражения (не кластеризуются) → заменяет coverage-гейт. ⚠️ **код MIT, но weights
  buffalo_l/antelopev2 NON-COMMERCIAL** (переобучить/лицензировать; MobileFaceNet ~13МБ = CPU-дёшево,
  эмбеддить только keyframes). Референс-стек в одном месте:
  https://github.com/vectornguyen76/face-recognition (SCRFD+ByteTrack+ArcFace).
- ⚠️ **Открытая экспозиция:** лицензия вендоренного **S3FD**-веса неизвестна — возможная УЖЕ
  существующая коммерческая проблема, проверить.

### 3.3 Auto-reframe системы — **AutoFlip (как НЕ надо) + EditIQ (как надо)**
- **Google AutoFlip** (Apache-2.0): fusion сигналов с флагом `is_required` (required-регион ОБЯЗАН
  влезть, best-effort — если помещается); `DecideCameraMotionType` → **STEADY / SWEEPING(пан) /
  TRACKING**; сглаживание `KinematicPathSolver` — **двух-пороговый dead-zone гистерезис** (порог входа
  ≠ порог выхода = анти-джиттер, велосити-eased, без снапа). **⚠️ AutoFlip НИКОГДА не режет внутри
  шота** — только HOLD/PAN/TRACK, смена только на гистограммной склейке. То есть (B) — за пределами
  AutoFlip. Полезно взять: `is_required`-регионы + kinematic dead-zone для follow (A).
- **EditIQ** (ACM IUI 2025, arXiv 2502.02172, editiq-ave.github.io) — **почти точный референс (B).**
  Из ОДНОГО статичного wide синтезирует 2ⁿ−1 виртуальных PTZ-«камер», выбирает шоты **DP-минимизацией
  энергии**: `E = Σ−ln(U) + Σ[Overlap + Rhythm + Transition] + Σ Misframing`. Unary U = LLM-контекст
  (Claude 3.5 читает субтитры → кто + после какого слова резать) × ViNet-саливенси × TalkNet-speaker.
  **Константы (CONFIRMED):** min-shot **l=1с**, avg **m=7с**, штраф ритма <1с **γ1=100**, Overlap-штраф
  **ν≈1e6** при IoU>β=0.30 (кусочно: 0 до α=0.15, линейно 0.15→0.30, потом стена 1e6 → «кропы у стены
  почти джамп-каты»). **Только жёсткие cut'ы, снап на word-таймстемп. Открывается 2с establishing-wide.**
  Рантайм ~1× длины видео на 4090 (доминируют LLM/саливенси; **DP-ядро — копеечный CPU**). ⚠️ **кода
  НЕТ → реимплемент по статье.**
- **Leake et al., Computational Video Editing for Dialogue** (SIGGRAPH 2017,
  graphics.stanford.edu/papers/roughcut/) — HMM/Viterbi-редактор с киноидиомами (start-wide,
  speaker-visible, avoid-short-clips=min-hold, avoid-jump-cuts, emphasize-speaker) = чек-лист правил.
- **Grundmann L1-Optimal Camera Paths** (CVPR'11) — пути только из Const-Position/Velocity/Accel
  сегментов («оператор», не «слайд») → замена lerp для intra-shot follow (A).
- **Продукты:** Premiere Auto Reframe (Sensei) — **ПАНИТ** keyframes, мульти-субъект → руками. FCP
  Smart Conform — статичный кроп, без трекинга/cut. Opus — Fill/Fit/**Split(только если оба видны)**,
  Pro «переключает фокус на говорящего» (cut vs pan **не документирован**).
- **OSS-реймеры** (все ПАНЯТ, никто не режет внутри шота): **auto-vertical-reframe (KazKozDev, MIT)** —
  ближайший шаблон к нашему stage3 (детект+ByteTrack+MediaPipe+ранжирование+PySceneDetect+path-опт),
  https://github.com/KazKozDev/auto-vertical-reframe ; smart-reframe (One-Euro фильтр).

### 3.4 Диаризация + audio↔face fusion — **вероятный backbone (B)**
- **Диаризаторы:** **pyannote 3.1** — **MIT**, HF-gated (токен), CPU-можно, DER VoxConverse 11.3 /
  AMI 18.8. ⚠️ **pyannote community-1/4.0 = CC-BY-4.0 (атрибуция), НЕ MIT** (verifier REFUTED); прирост
  над 3.1 маргинальный. **NeMo Streaming Sortformer** — CC-BY-4.0, frame-level T×4 вероятности
  (ложатся на 25fps-сетку), **жёсткий кап 4 спикера** (подкасту ок), GPU/стриминг. **WhisperX**
  (BSD-2, ⚠️ дефолтные wav2vec2-align модели бывают CC-BY-NC по языкам) → **per-WORD SPEAKER_k**;
  **самая дешёвая интеграция: у нас УЖЕ есть word-тайминги → маржинальная стоимость = один
  pyannote-проход.** Reverb v2 — NON-COMMERCIAL, исключить.
- **Fusion (привязка голоса к лицу):**
  - **TS-TalkNet** (см. 3.1) — enrollment каждого диар-кластера → выбираем лицо, чьё ASD совпадает с
    ЭТИМ голосом → решает ambiguous/profile/occluded момент.
  - **MERL Late AV Fusion** (arXiv 2211.01299) — **шаблон архитектуры:** держим диаризацию и визуальный
    ASD РАЗДЕЛЬНО, фьюзим **глобальным permutation-matching** кластер↔трек по времени; где ASD уверен —
    его скор замещает аудио; несматченное аудио остаётся аудио-only.
  - **Active-speaker faces for TV diarization** (arXiv 2203.15961) — **даже ~60%-точный ASD бьёт
    audio-only** (Friends DER 0.13 vs 0.17–0.27) → на LR-ASD-дорожки как первичную идентичность
    опираться можно.
  - **AVA-AVD** (arXiv 2111.14448) — бенчмарк на **off-screen речь**, наш eval-harness.
  - **Endpointing:** LiveKit turn-detector; **Next-Turn** (arXiv 2606.18094, +25.9% в 320мс) — VAD+
    просодия+семантика чтобы отличить 200мс-вдох от конца реплики и поставить cut на онсет.
- **Каноничный рецепт:** строим **персистентную таблицу `дорожка↔кластер`** на клип (какая ArcFace-lane
  ко-активна с каждым SPEAKER_k за весь клип); на рендере активное лицо = `binding[текущий_кластер]`,
  LR-ASD = рефайнер/тай-брейк, НЕ единоличный решала (гасит мерцание). **Off-screen** = у активного
  кластера нет привязанной видимой дорожки → hold/wide, не гнаться за призраком.

### 3.5 Конкуренты — **никто не делает speech-anchored cut из одной статичной камеры**
Три поведения (главный тезис CONFIRMED, контрпримера не нашли):
- **ПАН/трекинг («слоп», жалоба #1):** Opus Clip ReframeAnything (пан; Split только если оба видны;
  есть early-access diarization API), **Descript «Center active speaker» (beta)** (пан; сильнее с
  per-speaker мультитреком, деградирует на едином миксе), **Vizard/quso CutMagic** (диар-driven,
  «ок на 2, ломается на 3+»), Klap/Submagic (head-track), VEED/Kapwing.
- **SPLIT-SCREEN / stacked (обход переключения, нужны оба лица):** Choppity, Opus Split/Three/Four,
  Riverside grid.
- **Настоящий HARD CUT — только через реальный multicam/мультитрек:** **Eddie AI** (≤6 физ. камер),
  **Riverside «automatic speaker view»** (из изолированных пер-спикер треков). Виртуальной второй
  камеры из ОДНОГО wide — нет.
- **Жалоба #1:** на двух с перекрывающейся речью трекер прыгает на не того / пинг-понг. Независимый
  тест (autoposting.ai) ≈ **50% точности** на мульти-спикер панелях. ⚠️ Opus «pan-vs-cut» —
  **не документирован, проверить руками** прежде чем позиционировать (B) как отличие.
- **Мораль:** ров = надёжная **single-track атрибуция голос→лицо + debounced speech-boundary HARD CUT**
  — этого практически никто не отгружает.

### 3.6 Кинематографика — **CUT, НИКОГДА PAN (это решено, не вкусовщина)**
- **Tim Smith, Attentional Theory of Cinematic Continuity (AToCC)** (Projections 6(1), 2012): cut
  НЕВИДИМ, когда совпадает с саккадой/сдвигом внимания зрителя, а **онсет голоса нового спикера И ЕСТЬ
  этот триггер** → cut на онсете маскируется change-blindness'ом. Пан наоборот — заставляет
  разглядывать непрерывное, беспараллаксное, физически невозможное движение (реальная камера не панит
  между двумя сидящими) = «слоп».
- **Когда резать:** на **онсет речи** (в статичной камере аудио-сдвиг внимания — ЕДИНСТВЕННАЯ маска,
  прятать за контент-движением нечего). **J-cut** (аудио ведёт картинку) — про-приём; одновременный A/V
  наименее натурален; **L-cut** (задержаться на реакции текущего перед прыжком) де-механизирует быстрый
  диалог. ⚠️ Правильный lead для cut'а В ОДНОМ КАДРЕ в литературе НЕ описан — вероятно сильно меньше
  (несколько кадров ~0.1–0.2с), тюнить A/B.
- **180° / eyeline / shot-reverse-shot:** кропить каждого спикера смотрящим ВНУТРЬ (левый смотрит
  вправо-в-кадр, правый — влево), стабильное screen-direction; **различать кропы по масштабу/хедруму**,
  чтобы общий статичный фон меньше выдавал.
- **Прайор-арт:** EditIQ (§3.3); **GAZED** (⚠️ verifier: **CHI 2020**, не ACM MM) + **Real-Time GAZED**
  (arXiv 2311.15581); **Gaze-Driven Video Re-Editing** (CMU, ACM TOG 2015,
  graphics.cs.cmu.edu/projects/gazedriven/) — **каноничная формулировка «CUT когда цель ПРЫГАЕТ, PAN
  когда ДРЕЙФУЕТ»**; *Watch to Edit* (Eurographics 2018, arXiv 1807.03125). Leake-идиомы.
- **Анти-пинг-понг (решённый паттерн):** min-hold + transition-штраф (EditIQ l=1с). **Оффлайн → делаем
  LOOKAHEAD:** требуем, чтобы входящий спикер держал слово ≥ min-hold (~0.8–1.0с) ПРЕЖДЕ чем
  зафиксировать cut, затем ретроактивно ставим cut на его первое слово; бэкченелы («ага»/«да») порог не
  проходят → чище живого Шмитт-триггера (мы видим будущее).

---

## 4. Рекомендуемый signal stack

1. **ASD:** оставить **Light-ASD** (MIT, дёшево, робастно). НЕ свапать на LoCoNet/D²Stream. SPELL/GraVi-T
   исключить (non-commercial).
2. **Детект:** заменить вендоренный S3FD на **serengil/biubug6 RetinaFace (MIT)** default; оценить
   YOLO-FaceV2 (лучшее на соседей, но AGPL). Избегать SCRFD/buffalo (non-commercial) и Ultralytics
   YOLO (AGPL).
3. **Трекинг:** **ByteTrack** или **OC-SORT** (MIT); на статичной камере — position-anchored «seats».
4. **Identity binding:** **ArcFace** (self-trained/лицензированные веса) → K локнутых дорожек = высший
   рычаг (окклюзия + соседи + отражения). Блюпринт vectornguyen76/face-recognition.
5. **Диаризация:** **pyannote 3.1 (MIT)** или community-1 (**CC-BY-4.0, атрибуция**) через WhisperX-style
   word-labeling; NeMo Sortformer (≤4 спк, GPU) как альтернатива со стабильными arrival-order метками.
6. **Fusion:** персистентная таблица `дорожка↔кластер` (MERL 2211.01299 / constrained-clustering
   2408.12102); активное лицо = `binding[кластер]`, LR-ASD = рефайнер.
7. **Тай-брейк/вето:** MediaPipe jawOpen/lip-distance variance (Apache-2.0, CPU), audio-gated; опц.
   SyncNet; TS-TalkNet для ambiguous/profile момента.

---

## 5. Варианты дизайна переключения (4 опции)

**Опция 1 — Глобальный DP-селектор шотов (порт EditIQ).**
- Моделируем (B) как авто-МОНТАЖ, не reframe. Кандидат-«камеры» на кадр из наших готовых кропов
  {wide/fit, tight-A, tight-B, split-если-оба}. Unary U награждает кроп на активном спикере (fused
  кластер + LR-ASD + владелец слова), чуть награждает wide на кростоке/тишине. Минимизируем
  `E = Σ−ln(U) + Σ[Transition + Rhythm + Overlap(IoU)]` через DP+backtracking на весь клип. Открытие
  ~2с establishing-wide.
- *Тайминг:* снап cut'а на word-границу — первое слово входящего, или в >150–300мс пред-онсетную паузу
  (переиспользуем silences из pause-removal); опц. J-cut lead ~3–12 кадров.
- *Анти-пинг-понг:* Rhythm (жёсткий штраф <1с γ1=100) + Transition-штраф делают частые cut'ы глобально
  дорогими; min-shot l = мягкий min-hold; суб-hold реплики никогда не выигрывают.
- *Плюсы:* доказуемо не строобит (min-hold/каденс — ГЛОБАЛЬНЫЕ стоимости, не жадные пороги — жадность и
  есть причина строба); оптимум на весь клип; переиспользует наш frame-accurate hard-cut рендер;
  IoU-штраф даёт принципиальный «когда cut безопасен»; дёшево (полиномиально, CPU); высшая верность
  валидированному прайор-арту.
- *Минусы:* только оффлайн (нам ок); нужно построить DP-решётку + тюнить веса; **у EditIQ нет кода →
  реимплемент**; киношные константы (m=7с) тюнить короче под шортсы.
- *Прайор-арт:* EditIQ (2502.02172), Leake 2017.

**Опция 2 — Lookahead turn-scheduler с min-hold (эвристика, без DP).**
- Жадный-с-lookahead автомат. Диаризация даёт реплики; требуем ≥ min-hold (~1.0–1.5с) удержания
  ПРЕЖДЕ чем зафиксировать, потом ретроактивно ставим cut на первое слово. Off-screen/кросток → hold/wide.
- *Плюсы:* сильно меньше строить; lookahead чисто убивает пинг-понг + бэкченелы; один интуитивный кноб
  «min-hold»; низкий риск.
- *Минусы:* не глобальный оптимум (может резать на B ровно перед тем, как A вернёт слово); нет
  принципиального IoU-гейта, пока не прикрутишь → может пропустить глючный cut между почти-одинаковыми
  кропами.
- *Прайор-арт:* diar-driven multicam autocut (AutoCut/Descript/AutoPod); turn/endpoint (LiveKit, Next-Turn).

**Опция 3 — Geometry-gated гибрид: cut когда далеко, split/hold когда близко.**
- Кладём EditIQ-Overlap/IoU поверх Опции 1 или 2. Разрешаем hard-cut A→B ТОЛЬКО когда кропы достаточно
  РАЗНЫЕ (низкий IoU/далеко) → прыжок читается как отдельная камера; когда спикеры близко (высокий
  overlap → cut выглядит как глитч на общем фоне) → fill-follow / stacked-split / wide. Форсим 180°
  внутрь-eyeline + различие масштаба/хедрума.
- *Плюсы:* прямо бьёт киллер-риск одинакового фона; cut читается как reverse-shot; принципиальная
  грациозная деградация; композится с любым шедулером.
- *Минусы:* +слой геометрии/фолбэка; в тесных 2-shot'ах B часто ЗАПРЕЩЁН → на ровно той side-by-side
  съёмке, что просят клиенты, часто падаешь в split/fill.
- *Прайор-арт:* EditIQ Overlap-штраф; 180°/shot-reverse-shot; Smith AToCC.

**Опция 4 (не-дефолт / premium) — Per-scene LLM-режиссёр диалога.**
- LLM по транскрипту рекомендует кого показать + после какого слова резать (полный семантический слой
  EditIQ). Ловит реакции/эмфазу, что ASD упускает. Дорого/медленно (~1× видео на GPU, LLM-bound) →
  отложить в премиум.

---

## 6. Как сделать «будто вторая камера» (конкретика)

Cut у нас уже бесплатный. Искусство — в двух вещах:
1. **Момент cut'а** = диаризационная граница реплики ∩ онсет первого слова ∩ VAD/просодическая пауза,
   снапнутый на нативный кадр `round(t*fps)/fps`. Гистерезис = lookahead min-hold (§3.6), бэкченелы
   отсекаются.
2. **Кадрирование «другой камеры»** (иначе прыжок читается как «одну фотку подвинули»):
   - **IoU-гейт:** резать только если tight-A и tight-B достаточно различны (низкий IoU). Высокий IoU
     (близко сидят) → не cut, а split/wide.
   - **180° + eyeline:** A на одной трети смотрит внутрь, B на другой смотрит обратно = shot-reverse-shot.
   - **Различие масштаба/хедрума** между A и B — «две камеры стояли под разным углом», общий фон меньше
     выдаёт.
   - Обязательно проверять покадрово на реале — «одинаковый фон» в литературе для same-frame cut'а НЕ
     решён (риск №1, §9).

---

## 7. Интеграция в наш код (seams) + грабли

**Seam (A) — tight в multi-face:** после `_is_wide_shot` (`stage3_reframe.py:774`), если трек проходит
`wide_speak_min` (мёртвый кноб 0.3) → маршрут в fill (`_fill_points_for_target`), не в fit. НО в одиночку
= откаченное поведение → делать только ВМЕСТЕ с (B).

**Seam (B) — синтетический cut:** вставить switch-кадр в `cuts_face`/`cuts` в `reframe_segment`
(`~:1322`) **ДО** `score_tracks_in_segment`, чтобы `build_tracks._spans_cut` сбросил трекинг ровно там
(чистое разделение дорожек). Дальше `plan_regions` сам обрабатывает каждую реплику как под-шот и
`_pick_target` выбирает своего спикера. Рендер режет hard-cut.

**Диаризацию прокинуть:** `words: list[Word]` (с `.speaker`) в `reframe_segment`/`plan_regions` (сейчас
не течёт). Прайор-арт детекта смены спикера + снапа на паузу — `stage2_select._primary_speaker` /
`snap_end_index` (уже написаны, переиспользовать паттерн). Слои-мосты (диар↔lane binding) — новая чистая
функция.

**Грабли:**
1. **Grid Δ=0** (`REFRAME_FPS_GRID_INVARIANT.md`): switch-кадр ОБЯЗАН быть `round(t*fps)/fps` в нативном
   fps; на дробном fps (29.97/23.976) промах ±1 кадр = флеш. Тестить на НЕ-25fps реале.
2. **Baked-cache staleness:** `get_clip_reframe`/`plan_to_regions` отдают запечённые регионы →
   `update public.job_artifacts set reframe_regions = null;` после деплоя + чистить stale `clip_edits`.
3. **`merge_short_regions` (min_hold 1.5с) СЪЕДАЕТ короткие реплики** → нужен отдельный min-hold для
   speaker-switch под-регионов ИЛИ обход merge (как `keep_distant_cutaways`).
4. **ASD 25fps** — точность switch-кадра ограничена 25fps даже на 60fps-источнике.
5. **Coverage/spurious-гейты нагруженные** — не ослаблять пороги ради второго спикера (вернёт «ложный
   wide»/«ноги»); добавлять НОВУЮ ветку, а не ослаблять существующую.
6. **Per-user preference, НЕ глобальный флип** (основатель хочет wide по умолчанию). Как `remove_pauses`:
   колонка в `jobs` + сид в `ensure_edit` (`default_clip_edit`), фронт-тумблер + i18n en/ru.
7. **Fan-out:** диаризация — джоб-уровневая; в пер-клип контейнере доступна через
   `store.load_transcript_words` (Word.speaker) — прокинуть в reframe локально в контейнере.
8. **Лицензии весов — реальный гейт** (см. §3): SPELL non-commercial (искл.), InsightFace weights
   non-commercial, Ultralytics/YOLOv5 AGPL, pyannote community-1 CC-BY-4.0 (атрибуция), WhisperX
   wav2vec2 бывает CC-BY-NC, **вендоренный S3FD — неизвестно (проверить, возможна прошлая экспозиция).**

---

## 8. Фазировка (предложение)

- **P0 (read-only):** прокинуть диаризацию в reframe; построить `дорожка↔кластер` binding-таблицу
  (ArcFace-lane или пока IOU-lane + ASD-аккумуляция); DEBUG-дамп «кто/когда/какое лицо» на реальном
  2-person подкасте. Ничего не рендерим — валидируем атрибуцию.
- **P1:** per-user тумблер «Speaker focus / Tight» (default OFF); синтетические cut'ы по диар-границам +
  per-turn `_pick_target`; lookahead min-hold + снап на паузу/первое-слово + grid-snap; IoU/eyeline-гейт;
  фолбэк wide/split. TDD на чистой логике (turn-detect + hysteresis + snap + grid).
- **P2:** eyeline-aware framing (shot-reverse-shot, различие масштаба); DP-селектор (EditIQ-порт) если
  эвристика P1 недостаточна.
- **P3:** split-screen фолбэк для 3+/ambiguous; ArcFace-binding если IOU-lane мало.
- **P4 (premium):** LLM-режиссёр (Опция 4); опц. LoCoNet/TalkNCE для hard-case.

---

## 9. Риски + чему НЕ доверять

**Риски:**
1. **Одинаковый фон** (оба кропа из ОДНОГО кадра, без параллакса) → даже идеальный тайминг может читаться
   как «фотку подвинули». Митигация: IoU-гейт + 180° + различие масштаба; **проверять глазами** (в
   литературе для same-frame cut'а не решено).
2. **In-the-wild ASD-коллапс** (95%→76–83%, UniTalk) — чистые рендеры/юниты не покажут. Нужен свой
   eval-сет реальных 2-person подкастов (профиль/кросток/отражения): точность выбора спикера + офсет cut'а.
3. **Off-screen + перекрывающаяся речь** — универсальный провал; нет привязанной видимой дорожки → hold/
   wide, не гнаться за призраком. WhisperX = один спикер на слово, оверлап не делит.
4. **Лицензии** (см. §7.8) — решить по каждому весу ДО шипа.
5. **EditIQ без кода** — Опция 1 = реимплемент; константы (7с) тюнить; правильный J-cut lead для
   same-frame неизвестен → A/B.
6. **CPU/латентность** — «SCRFD 5мс CPU» = GPU-число; полный детект+ByteTrack+ArcFace НЕ CPU-реалтайм →
   модест-GPU или sparse keyframes.
7. **Диаризация = сильный prior, не ground truth** (AMI DER ~19%, AVA-AVD ~45%) — кросс-чек ArcFace +
   min-hold; никогда не доверять мгновенному ASD/largest-face (провал Klap/Submagic).

**Verifier REFUTED / сомнительное (design against, не цитировать как факт):**
- SPELL/GraVi-T = **NON-COMMERCIAL** (не Apache) → исключить.
- Light-ASD = **MIT** (благоприятно).
- «SCRFD 5мс CPU» — GPU-числа; 10GF ≈ >100мс/кадр CPU.
- pyannote community-1/4.0 = **CC-BY-4.0** (атрибуция), НЕ MIT (только 3.1 = MIT).
- GAZED venue = **CHI 2020**, не ACM MM.
- **Opus Clip cut-vs-pan на смене спикера — НЕ документирован** нигде → проверить руками прежде чем
  позиционировать (B) как отличие.
- Мягкое/UNCERTAIN: Light-ASD 0.84M/0.51GFLOP (over-precise → ~1M/sub-1GFLOP); competitor «25–30%
  manual-fix» (не подтверждён; жёстче ~50%); Klap/Submagic «largest-face» (один конфликтный вендор-блог);
  Choppity stacked-механика (инференс); MERL 47.6→45.1 DER (PDF не извлекается).

---

## 10. Ключевые источники
- EditIQ — arXiv 2502.02172 · editiq-ave.github.io · dl.acm.org/doi/10.1145/3708359.3712113
- AutoFlip — github.com/google-ai-edge/mediapipe (examples/desktop/autoflip)
- Leake 2017 — graphics.stanford.edu/papers/roughcut/ · Grundmann L1 — research.google.com/pubs/archive/37041.pdf
- Gaze-Driven Re-Editing (CMU TOG'15) — graphics.cs.cmu.edu/projects/gazedriven/ · Real-Time GAZED arXiv 2311.15581 · Watch-to-Edit arXiv 1807.03125
- Smith AToCC — berghahnjournals.com/view/journals/projections/6/1/proj060102.xml
- Light-ASD — github.com/Junhua-Liao/LR-ASD · LoCoNet — github.com/SJTUwxz/LoCoNet_ASD · TalkNCE — github.com/kaistmm/TalkNCE · TS-TalkNet — github.com/Jiang-Yidi/TS-TalkNet (arXiv 2305.12831)
- UniTalk (2025) · AVA-AVD arXiv 2111.14448 · MERL fusion arXiv 2211.01299 · TV-diar arXiv 2203.15961 · constrained-cluster arXiv 2408.12102 · Next-Turn arXiv 2606.18094
- RetinaFace(MIT) — github.com/serengil/retinaface, github.com/biubug6/Pytorch_Retinaface · YOLO-FaceV2 arXiv 2208.02019 · ByteTrack — github.com/ifzhang/ByteTrack · OC-SORT — github.com/noahcao/OC_SORT · ArcFace/InsightFace — github.com/deepinsight/insightface · full-stack — github.com/vectornguyen76/face-recognition
- pyannote — huggingface.co/pyannote/speaker-diarization-3.1 · NeMo Sortformer — huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2 · WhisperX — github.com/m-bain/whisperX
- OSS reframer — github.com/KazKozDev/auto-vertical-reframe
