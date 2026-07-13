# face-reID weights — SFace (OpenCV Zoo)

`face_recognition_sface_2021dec.onnx` — 36.9 MB, MobileFaceNet, вход 1×3×112×112 → **128-d**.

| | |
|---|---|
| **Лицензия весов** | **Apache-2.0** — `LICENSE` в этой папке (копия из апстрима, как требует Apache при редистрибуции: мы кладём веса в Docker-образ) |
| Источник | <https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface> |
| Точность | LFW 99.40% |
| Рантайм | `cv2.FaceRecognizerSF` — **новых зависимостей НЕТ** (cv2 уже есть; torch/torchvision для reID НЕ нужны) |

**Почему грант настоящий, а не boilerplate:** OpenCV Zoo назначает лицензию **ПОКАТАЛОЖНО** — у
соседнего YuNet `LICENSE` = MIT, у SFace = Apache-2.0. README апстрима: *«All files in this directory
are licensed under Apache 2.0»*, а `.onnx` лежит ровно в той директории.

**Отвергнутые альтернативы** (ресёрч 2026-07-13 — читались сами LICENSE/model-cards, не README):
InsightFace `buffalo_l`/`antelopev2` — веса **non-commercial** (они продают коммерческую лицензию);
EdgeFace — код BSD-3, но веса **CC BY-NC-SA**; facenet-pytorch (VGGFace2/CASIA) — research-only;
`onnx/models` ArcFace — помечен Apache, но обучен на **отозванном** MS-Celeb-1M (ярлык врёт).
Запасной вариант с самой чистой родословной: Intel `face-reidentification-retail-0095`
(Apache-2.0, обучен самим Intel), ценой зависимости `openvino`.

⚠️ Веса **лежат в git** намеренно: `Dockerfile` делает `COPY app` → скачивание в рантайме не нужно
(нет сетевой точки отказа на проде). Обновление файла = обновление и этого README.
