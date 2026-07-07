"""Multipart-upload part planning (pure logic) + list_parts (resume/renewal).

Большие загрузки (>5 ГБ single-PUT R2) режутся на части браузером и собираются R2 multipart.
``plan_part_count`` — сколько частей нужно для файла размера ``size`` при ``part_size``.
``list_parts`` — какие части УЖЕ в R2 (ListParts), для resume/renewal presigned-URL'ов.
"""

from __future__ import annotations

import pytest

from app import storage
from app.errors import JobError
from app.storage import MULTIPART_PART_SIZE, plan_part_count


def test_small_file_is_one_part() -> None:
    assert plan_part_count(1, MULTIPART_PART_SIZE) == 1
    assert plan_part_count(MULTIPART_PART_SIZE, MULTIPART_PART_SIZE) == 1


def test_exact_multiple() -> None:
    assert plan_part_count(3 * MULTIPART_PART_SIZE, MULTIPART_PART_SIZE) == 3


def test_remainder_rounds_up() -> None:
    assert plan_part_count(2 * MULTIPART_PART_SIZE + 1, MULTIPART_PART_SIZE) == 3


def test_zero_or_negative_is_one_part() -> None:
    # Defensive: a bad/zero size still yields a valid single-part plan, never 0 parts.
    assert plan_part_count(0, MULTIPART_PART_SIZE) == 1
    assert plan_part_count(-10, MULTIPART_PART_SIZE) == 1


# ─────────────────────────── B1: list_parts (ListParts wrapper) ───────────────────────────


def test_list_parts_normalizes_and_sorts(monkeypatch) -> None:
    # ListParts может вернуть части ВРАЗНОБОЙ и с S3-именами ключей (PartNumber/ETag/Size);
    # list_parts нормализует к {part_number,etag,size} и сортирует по part_number.
    class _FakeClient:
        def list_parts(self, **kwargs):
            assert kwargs["UploadId"] == "up1"
            return {
                "Parts": [
                    {"PartNumber": 3, "ETag": '"e3"', "Size": 16},
                    {"PartNumber": 1, "ETag": '"e1"', "Size": 32},
                ],
                "IsTruncated": False,
            }

    monkeypatch.setattr(storage, "_r2_client", lambda: _FakeClient())
    parts = storage.list_parts("job_x", "up1")
    assert parts == [
        {"part_number": 1, "etag": '"e1"', "size": 32},
        {"part_number": 3, "etag": '"e3"', "size": 16},
    ]


def test_list_parts_paginates(monkeypatch) -> None:
    # >1000 частей у R2/S3 приходят страницами (IsTruncated + NextPartNumberMarker); собираем ВСЕ.
    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def list_parts(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "Parts": [{"PartNumber": 1, "ETag": "e1", "Size": 10}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                }
            assert kwargs.get("PartNumberMarker") == 1
            return {"Parts": [{"PartNumber": 2, "ETag": "e2", "Size": 20}], "IsTruncated": False}

    fc = _FakeClient()
    monkeypatch.setattr(storage, "_r2_client", lambda: fc)
    parts = storage.list_parts("job_x", "up1")
    assert [p["part_number"] for p in parts] == [1, 2]
    assert fc.calls == 2


def test_list_parts_empty_when_no_parts(monkeypatch) -> None:
    class _FakeClient:
        def list_parts(self, **kwargs):
            return {"IsTruncated": False}  # ни одной части ещё не залито

    monkeypatch.setattr(storage, "_r2_client", lambda: _FakeClient())
    assert storage.list_parts("job_x", "up1") == []


def test_list_parts_raises_joberror_on_failure(monkeypatch) -> None:
    # Правило №8: сбой R2 → JobError, НЕ тихий пустой список (иначе resume «потеряет» части).
    class _BoomClient:
        def list_parts(self, **kwargs):
            raise RuntimeError("R2 down")

    monkeypatch.setattr(storage, "_r2_client", lambda: _BoomClient())
    with pytest.raises(JobError):
        storage.list_parts("job_x", "up1")
