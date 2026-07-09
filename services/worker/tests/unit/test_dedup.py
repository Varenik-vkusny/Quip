"""Тесты чистого ядра per-user дедупа исходников (app.dedup). Zero mocks.

``is_valid_fingerprint`` — валидация ``<size>:<sha256-hex>``-отпечатка (сайд-канал от клиента,
доверять нельзя). ``pick_duplicate_candidate`` — детерминированный выбор «самого свежего» из
собственных прошлых джоб юзера (самый свежий = лучший шанс, что его source ещё жив в R2 под
60-дневным ретеншном).
"""

from __future__ import annotations

from app.dedup import is_valid_fingerprint, pick_duplicate_candidate

# ─────────────────────────── is_valid_fingerprint ───────────────────────────


def test_valid_fingerprint_accepts_size_and_lowercase_sha256() -> None:
    assert is_valid_fingerprint("12345:" + "a" * 64)
    # size = один ноль (1 знак), хэш = 64 валидных hex-символа lowercase
    assert is_valid_fingerprint("0:" + "0123456789abcdef" * 4)


def test_valid_fingerprint_rejects_uppercase_hex() -> None:
    # sha256 в отпечатке ДОЛЖЕН быть lowercase (клиент нормализует) — заглавные отвергаем.
    assert not is_valid_fingerprint("100:" + "A" * 64)


def test_valid_fingerprint_rejects_wrong_hash_length() -> None:
    assert not is_valid_fingerprint("100:" + "a" * 63)  # 63 < 64
    assert not is_valid_fingerprint("100:" + "a" * 65)  # 65 > 64


def test_valid_fingerprint_rejects_missing_colon() -> None:
    assert not is_valid_fingerprint("100" + "a" * 64)


def test_valid_fingerprint_rejects_empty_and_garbage() -> None:
    assert not is_valid_fingerprint("")
    assert not is_valid_fingerprint(":")
    assert not is_valid_fingerprint("abc:" + "a" * 64)  # size — не цифры
    assert not is_valid_fingerprint("100:" + "g" * 64)  # 'g' — не hex


def test_valid_fingerprint_rejects_oversized_size_field() -> None:
    # size — не более 20 знаков (int64-разрядность байтов); 21 знак отвергаем.
    assert not is_valid_fingerprint("1" * 21 + ":" + "a" * 64)


# ─────────────────────────── pick_duplicate_candidate ───────────────────────────


def test_pick_candidate_empty_is_none() -> None:
    assert pick_duplicate_candidate([]) is None


def test_pick_candidate_single() -> None:
    assert pick_duplicate_candidate([{"id": "job_a", "created_at": 1.0}]) == "job_a"


def test_pick_candidate_most_recent_wins() -> None:
    cands = [
        {"id": "job_old", "created_at": 100.0},
        {"id": "job_new", "created_at": 300.0},
        {"id": "job_mid", "created_at": 200.0},
    ]
    assert pick_duplicate_candidate(cands) == "job_new"


def test_pick_candidate_tiebreak_is_deterministic() -> None:
    # Равный created_at → выбор детерминирован (id как тай-брейк), не зависит от порядка входа.
    cands = [
        {"id": "job_a", "created_at": 50.0},
        {"id": "job_b", "created_at": 50.0},
    ]
    picked = pick_duplicate_candidate(cands)
    assert picked == pick_duplicate_candidate(list(reversed(cands)))
    assert picked in {"job_a", "job_b"}
    # конкретно — самый большой id (стабильный порядок)
    assert picked == "job_b"
