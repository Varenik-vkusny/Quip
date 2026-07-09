"""Polar.sh интеграция (P1): проверка подписи вебхука (Standard Webhooks) + маппинг
product→план. Всё PURE и покрыто тестами (включая официальный тест-вектор Standard
Webhooks) — провод/секреты живут в main.py + config.

Polar шлёт вебхуки по спецификации **Standard Webhooks** (как svix):
заголовки ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``; секрет
``whsec_<base64>``; подписывается ``"{id}.{timestamp}.{body}"`` через HMAC-SHA256,
результат base64; заголовок подписи — список ``v1,<sig>`` через пробел (любой матч).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import Any

# События подписки, после которых план = product (активная подписка).
_ACTIVE_EVENTS = frozenset(
    {
        "subscription.created",
        "subscription.active",
        "subscription.updated",
        "subscription.uncanceled",
    }
)
# События, после которых план сбрасывается во free.
_DOWNGRADE_EVENTS = frozenset({"subscription.canceled", "subscription.revoked"})
# Разовая оплата (PAYG-кредиты): одноразовый заказ, НЕ подписка.
_ORDER_PAID_EVENTS = frozenset({"order.created", "order.paid", "order.updated"})


def verify_signature(
    secret: str,
    body: bytes | str,
    *,
    webhook_id: str,
    webhook_timestamp: str,
    signature_header: str,
    tolerance_sec: int = 300,
    now: int | None = None,
) -> bool:
    """True ⇔ подпись валидна по Standard Webhooks. PURE (``now`` инъектируется в тестах).

    Проверяет HMAC-SHA256 над ``"{id}.{timestamp}.{body}"`` ключом base64-decode(secret
    без ``whsec_``), constant-time против записей ``v1,<sig>`` в заголовке. Плюс окно
    времени (анти-replay): |now − timestamp| ≤ tolerance_sec.
    """
    if not (secret and webhook_id and webhook_timestamp and signature_header):
        return False
    try:
        ts = int(webhook_timestamp)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > tolerance_sec:
        return False

    # Вывод HMAC-ключа из секрета. ⚠️ КРИТИЧНО: Polar (secret ``polar_*``) и чистый Standard
    # Webhooks (``whsec_``) делают это ПО-РАЗНОМУ:
    #   • Polar: ключ = СЫРЫЕ UTF-8 байты ВСЕГО секрета (с префиксом). Его SDK base64-КОДИРУЕТ
    #     строку секрета и отдаёт в standardwebhooks, который base64-ДЕКОДИРУЕТ обратно →
    #     ровно ``secret.encode()``. (Раньше мы снимали префикс + base64-декодировали — НЕВЕРНЫЙ
    #     ключ → ВСЕ реальные вебхуки Polar отбивались 401; самоподписанный тест давал ложный 200,
    #     т.к. подписывал тем же неверным ключом. Доказано delivery-логом: 401×10.)
    #   • Standard Webhooks (svix): снять ``whsec_`` + base64-decode остатка.
    if secret.startswith("polar_"):
        key = secret.encode("utf-8")
    else:
        raw = secret[len("whsec_") :] if secret.startswith("whsec_") else secret
        raw += "=" * (-len(raw) % 4)  # терпим к недостающему base64-паддингу
        try:
            key = base64.b64decode(raw)
        except (ValueError, binascii.Error):
            return False

    payload = body.decode("utf-8") if isinstance(body, bytes) else body
    signed = f"{webhook_id}.{webhook_timestamp}.{payload}".encode()
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    for entry in signature_header.split():
        sig = entry.partition(",")[2] if "," in entry else entry
        if hmac.compare_digest(expected, sig):
            return True
    return False


def product_to_plan(product_id: str, *, starter: str, pro: str) -> str | None:
    """``product_id`` (Polar) → "starter" / "pro" / None (не наш продукт). PURE."""
    if starter and product_id == starter:
        return "starter"
    if pro and product_id == pro:
        return "pro"
    return None


# Платные планы, назначаемые подпиской через metadata.plan продукта в Polar
# (зеркалит платные ключи billing.PLANS). Новый платный тариф → добавь и СЮДА,
# и в billing.PLANS, и на фронт lib/plans.ts. "free" подпиской не назначается.
_SUBSCRIPTION_PLANS = frozenset({"starter", "pro", "studio"})


def _metadata_plan(data: dict[str, Any]) -> str | None:
    """Фолбэк-маппинг плана через ``metadata.plan`` продукта/заказа (forward-compat:
    фаундер выставляет metadata.plan=starter|pro|studio на продукте в Polar → маппим по нему
    БЕЗ env product_id). PURE."""
    for src in (data.get("product"), data):
        if isinstance(src, dict):
            meta = src.get("metadata")
            if isinstance(meta, dict):
                plan = str(meta.get("plan", "")).lower()
                if plan in _SUBSCRIPTION_PLANS:
                    return plan
    return None


def _extract_user_id(data: dict[str, Any]) -> str | None:
    """user_id из Polar-payload: customer.external_id (предпочтительно), затем
    metadata.user_id, затем customer_external_id на уровне подписки."""
    customer = data.get("customer")
    if isinstance(customer, dict) and customer.get("external_id"):
        return str(customer["external_id"])
    meta = data.get("metadata")
    if isinstance(meta, dict) and meta.get("user_id"):
        return str(meta["user_id"])
    ext = data.get("customer_external_id") or data.get("external_customer_id")
    return str(ext) if ext else None


def parse_plan_change(
    payload: dict[str, Any], *, product_starter: str, product_pro: str
) -> tuple[str, str] | None:
    """Из Polar webhook-payload достать ``(user_id, plan_id)``. PURE.

    Активная подписка наших продуктов → план starter/pro; canceled/revoked → "free".
    None, если ивент не про план, нет user_id, или продукт не наш (вебхук игнорируем —
    отвечаем 200, Polar не ретраит).
    """
    event = str(payload.get("type", ""))
    if event not in _ACTIVE_EVENTS and event not in _DOWNGRADE_EVENTS:
        return None
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    user_id = _extract_user_id(data)
    if not user_id:
        return None
    if event in _DOWNGRADE_EVENTS:
        return (user_id, "free")
    plan = product_to_plan(
        str(data.get("product_id", "")), starter=product_starter, pro=product_pro
    )
    if plan is None:
        plan = _metadata_plan(data)  # фолбэк: metadata.plan на продукте/заказе
    if plan is None:
        return None
    return (user_id, plan)


def _parse_one_time_order(
    payload: dict[str, Any], *, product_ids: str, credits_per_order: int
) -> tuple[str, int] | None:
    """Общий разбор разовой оплаты Polar → ``(user_id, credits)``. PURE.

    Заказ (НЕ подписка) продукта из ``product_ids`` начисляет ``credits_per_order`` кредитов
    (× quantity заказа, если задан). ``product_ids`` — id или СПИСОК id через запятую (заказ
    любого из них засчитывается). Матч строго по ``data.product_id`` (он всегда в payload) →
    разные балансы (PAYG-видео vs extra-clip) НЕ путаются: каждый вызывающий передаёт СВОЙ набор
    id. None, если ивент не про разовую оплату, продукт не в наборе, или нет user_id.
    """
    event = str(payload.get("type", ""))
    if event not in _ORDER_PAID_EVENTS:
        return None
    ids = {p.strip() for p in product_ids.split(",") if p.strip()}
    if not ids:
        return None
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    if str(data.get("product_id", "")) not in ids:
        return None
    user_id = _extract_user_id(data)
    if not user_id:
        return None
    qty = data.get("quantity")
    units = int(qty) if isinstance(qty, int) and qty > 0 else 1
    return (user_id, credits_per_order * units)


def parse_payg_order(
    payload: dict[str, Any], *, product_payg: str, credits_per_order: int = 1
) -> tuple[str, int] | None:
    """Из Polar webhook-payload разовой оплаты PAYG достать ``(user_id, credits)``. PURE.

    PAYG = одноразовый заказ продукта ``product_payg`` (НЕ подписка) → начисляем
    ``credits_per_order`` НЕ-сгорающих ВИДЕО-кредитов (с учётом quantity заказа, если задан).
    ``product_payg`` может быть списком id через запятую (основной $3 + тестовый $1 и т.п.).
    Начисляется в profiles.payg_credits (пул на обработку видео). None, если не наш заказ.
    """
    return _parse_one_time_order(
        payload, product_ids=product_payg, credits_per_order=credits_per_order
    )


def parse_extra_clip_order(
    payload: dict[str, Any], *, product_extra_clip: str, credits_per_order: int = 1
) -> tuple[str, int] | None:
    """Из Polar webhook-payload разовой оплаты «лишнего клипа» достать ``(user_id, credits)``. PURE.

    Extra-clip = одноразовый заказ продукта ``product_extra_clip`` ($1, НЕ подписка) → начисляем
    ``credits_per_order`` КЛИП-кредитов (с учётом quantity заказа). Это ОТДЕЛЬНЫЙ баланс
    profiles.clip_credits: клип-кредит тратится ТОЛЬКО на извлечение клипа за free-cap (см.
    main.extract_clip), НЕ на обработку целого видео. Разделение балансов закрывает эксплойт
    «$1 → $3-видео»: extra-clip и PAYG матчатся по РАЗНЫМ product_id. None, если не наш заказ.
    """
    return _parse_one_time_order(
        payload, product_ids=product_extra_clip, credits_per_order=credits_per_order
    )
