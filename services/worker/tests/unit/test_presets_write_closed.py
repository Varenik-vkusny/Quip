"""`POST /presets` был ОТКРЫТ ВСЕМУ ИНТЕРНЕТУ — единственный write-эндпоинт без авторизации.

Найдено аудитом 2026-07-14. Ручка не имела НИ `authorization`, НИ `x_user_id`, НИ вызова
`_require_*`, а Cloud Run поднят с `--allow-unauthenticated` (авторизация — на уровне приложения).
Она писала в ГЛОБАЛЬНЫЙ `presets.json` (`DATA_ROOT`), который `GET /presets` отдаёт ВСЕМ юзерам:

  • вандализм — любой мог залить пресеты с произвольными именами, и их видели ВСЕ в редакторе;
  • раздувание файла без лимита (и его парсинг на каждом `GET /presets`).

Фронт эту ручку НЕ ВЫЗЫВАЛ НИКОГДА (только `GET /presets` на чтение), а живая фича «Мои шаблоны»
живёт в `profiles.style_preferences` (per-user). Плюс в облаке запись всё равно уходила на
ЭФЕМЕРНЫЙ диск и пропадала при перезапуске контейнера — то есть ручка была ещё и нерабочей.
Мёртвый код с дырой → УДАЛЁН, а не «прикрыт токеном»: авторизовывать нечего, если ручку не зовут.

Этот тест пиннит, что ручка НЕ ВЕРНЁТСЯ: чтение живо, запись закрыта.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_get_presets_still_works() -> None:
    """Чтение живо — его зовёт PresetStrip во фронте."""
    r = client.get("/presets")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_post_presets_is_not_accepted() -> None:
    """Запись закрыта. 405 (метод не разрешён на существующем пути) — НЕ 200 и НЕ 401.

    Важно, что это НЕ 401: 401 означал бы «ручка есть, принеси токен», т.е. её можно вызвать с любым
    валидным токеном ЛЮБОГО юзера и всё равно записать в ГЛОБАЛЬНЫЙ файл. Её просто нет.
    """
    r = client.post("/presets", json={"name": "pwned", "style": {}})
    assert r.status_code == 405
