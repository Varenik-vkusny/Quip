"""Машина B (стадия РЕЗ): детерминированная дисциплина внешних границ клипа IN/OUT. PURE.

Спека: docs/superpowers/specs/2026-07-11-viral-moment-selection-design.md §6.

Задача: клип НЕ должен ОТКРЫВАТЬСЯ словом-связкой/дейктиком/филлером (бан-лист) или висячей
отсылкой (местоимение без антецедента в окне). Прямой кейс из A/B «виральный vs наш»: наш clip_10
стартовал с «Тут я сначала акцентировал внимание…» — грамматически это чистое начало предложения
(снаппер `snap_start_index` его пропускал), но «Тут» — дейктик-зачин, ноль зацепки → свайп.

Модуль — LEAF (никаких импортов из stage2_select → нет цикла). Здесь ЖИВЁТ примитив детекции
конца предложения (`_is_sentence_end`); stage2_select импортирует его отсюда (одна функция — один
дом, правило CLAUDE.md «дублируется логика → выноси в ОДНУ функцию»).

Только ВНЕШНИЕ границы (start-на-хуке / конец-на-«кнопке»). Внутренняя чистка пауз/филлеров тела
клипа — сознательно ВНЕ scope (отдельный проект «паузы»).
"""

from __future__ import annotations

from app.models import Word

_SENT_END = (".", "!", "?")
# симметрично _is_filler в stage2_select: снимаем окаймляющую пунктуацию, регистр, пробелы.
_PUNCT = ".,!?;:—–-\"'»«…()"


def _norm(text: str) -> str:
    """Нормализовать токен для сверки с бан-листами (trim, lower, снять пунктуацию). PURE."""
    return text.strip().lower().strip(_PUNCT).strip()


def _is_sentence_end(text: str) -> bool:
    """Слово завершает предложение (.?!), с учётом закрывающих кавычек. PURE."""
    return text.strip().rstrip("\"'»").endswith(_SENT_END)


# ─────────────────────────── бан-листы зачина (спека §6.4) ───────────────────────────
# Два тира — РАЗНАЯ стратегия реснэпа (см. snap_start_off_banlist):
#
# JUMP: дейктик / причинно-следственная / «вода»-фраза, где ВСЁ предложение — throat-clearing
# («тут я сначала акцентировал…», «то есть…», «дело в том что…», «потому что…»). Реснэп прыгает на
# СЛЕДУЮЩЕЕ предложение (внутри текущего — только зачин мысли, payoff'а нет).
BANLIST_JUMP: frozenset[str] = frozenset(
    {
        # RU
        "тут", "здесь", "поэтому", "потому что", "то есть", "то бишь", "так что",
        "как я говорил", "как уже сказал", "вот почему", "дело в том что", "вот тут", "тут вот",
        # EN
        "as i said", "like i said", "as i mentioned", "which means", "that's why",
        "the thing is", "one of the things", "okay so",
    }
)  # fmt: skip

# TRIM: лёгкий филлер/союз В НАЧАЛЕ сильного предложения («Вы знаете, 65%…», «Ну, дети берут…»,
# «So, 65%…»). Реснэп отрезает ТОЛЬКО сами филлер-токены вперёд к следующему слову ТОГО ЖЕ
# предложения — сильный контент (число/факт) сохраняется. Прыжок тут потерял бы payoff.
BANLIST_TRIM: frozenset[str] = frozenset(
    {
        # RU
        "и", "а", "но", "или", "ну", "вот", "значит", "короче", "типа", "зато", "ведь", "кстати",
        "так", "да", "вы знаете", "знаете", "слушайте", "смотрите", "ну и", "а ещё", "а потом",
        "более того", "опять же", "кроме того", "к тому же", "э", "ээ", "эээ", "эм",
        # EN
        "so", "and", "but", "or", "well", "um", "uh", "like", "anyway", "basically",
        "you know", "i mean",
    }
)  # fmt: skip

# HARD = объединение (banned_opener_len проверяет по нему; тир решает стратегию).
BANLIST_HARD: frozenset[str] = BANLIST_JUMP | BANLIST_TRIM

# BARE_REFS: голые местоимения/указатели. НЕ hard-бан (могут начинать валидный хук: «Это безумие»),
# а сигнал для coref-проверки: если антецедент вне окна, клип не понятен «с нуля».
BARE_REFS: frozenset[str] = frozenset(
    {
        # RU
        "это", "этот", "эта", "эти", "тот", "та", "те", "он", "она", "они", "оно",
        "там", "тогда", "туда", "этому", "этим", "того", "том",
        # EN
        "that", "this", "it", "he", "she", "they", "those", "these", "there", "then",
        "him", "her",
    }
)  # fmt: skip

_MAX_BAN_PHRASE = max(len(p.split()) for p in BANLIST_HARD)


def banned_opener_len(words: list[Word], start_idx: int) -> int:
    """Сколько ВЕДУЩИХ слов клипа образуют HARD-запрещённый зачин (0 = зачин чистый). PURE.

    Матчим самую ДЛИННУЮ фразу (жадно): «как я говорил» = 3, «то есть» = 2, «тут» = 1.
    """
    n = len(words)
    for span in range(_MAX_BAN_PHRASE, 0, -1):
        if start_idx + span > n:
            continue
        phrase = " ".join(_norm(words[start_idx + k].text) for k in range(span))
        if phrase in BANLIST_HARD:
            return span
    return 0


def _next_sentence_start(words: list[Word], si: int, end: int) -> int | None:
    """Индекс начала СЛЕДУЮЩЕГО предложения в [si, end] (после ближайшего .?!). None — нет. PURE."""
    for k in range(si, end + 1):
        if _is_sentence_end(words[k].text):
            nxt = k + 1
            return nxt if nxt <= end else None
    return None


def snap_start_off_banlist(
    words: list[Word], start_idx: int, end_idx: int | None = None, *, max_hops: int = 6
) -> int:
    """Если зачин HARD-запрещён — сдвинуть старт вперёд по стратегии тира. PURE.

    • JUMP-зачин (дейктик/«вода»-фраза) → прыжок на СЛЕДУЮЩЕЕ предложение (весь зачин — throat-
      clearing). Нет .?! впереди → за сами токены (не схлопывая клип).
    • TRIM-зачин (лёгкий филлер) → сдвиг ровно за филлер-токены к следующему слову ТОГО ЖЕ
      предложения (сильный контент сохраняется).
    До `max_hops` шагов — филлеры могут идти цепочкой («ну вот 65%…»). Никогда не пересекаем
    `end_idx`. Чистый зачин → без изменений (гардрейл срабатывает только на плохом старте).
    """
    end = end_idx if end_idx is not None else len(words) - 1
    si = start_idx
    for _ in range(max_hops):
        blen = banned_opener_len(words, si)
        if blen == 0:
            return si
        phrase = " ".join(_norm(words[si + k].text) for k in range(blen))
        if phrase in BANLIST_JUMP:
            nxt = _next_sentence_start(words, si, end)
            si = nxt if nxt is not None else min(si + blen, end)
        else:  # TRIM: за филлер-токены, оставаясь в том же предложении
            si = min(si + blen, end)
        if si >= end:
            return end
    return si


def opening_is_bare_ref(words: list[Word], start_idx: int) -> bool:
    """Первое слово клипа — голая отсылка (местоимение/указатель) → возможна висячая ссылка. P."""
    return _norm(words[start_idx].text) in BARE_REFS
