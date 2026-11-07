"""Разбор занятия: данные и интерфейс составителя текста.

Граница проведена здесь сознательно. Вердикты выносит сценарный движок — это
детерминированное решение по правилам, которое можно предъявить на приёмке и
воспроизвести прогоном. Составитель разбора получает уже готовые вердикты и
только пересказывает их связным текстом.

Поэтому любая реализация обязана соблюдать правило: **новых оценок не
появляется**. Если в занятии одна ошибка, в разборе не может быть двух.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DebriefEvent:
    """Одна оценка из журнала занятия, приведённая к человеческому виду."""

    at_sec: float
    """Секунда от начала занятия."""

    step_title: str
    kind: str
    """MATCH | MISMATCH | UNCLEAR | TIMEOUT."""

    severity: str
    reason: str
    message: str
    consequence: str | None = None
    """Последствие ошибки по регламенту, если шаг его описывает."""


@dataclass(frozen=True, slots=True)
class DebriefInput:
    """Всё, что известно о занятии к моменту разбора."""

    trainee_name: str
    scenario: str
    mode: str
    state: str
    duration_sec: float
    steps_done: int
    steps_total: int
    errors: int
    events: list[DebriefEvent] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.steps_total > 0 and self.steps_done >= self.steps_total

    def problems(self) -> list[DebriefEvent]:
        return [e for e in self.events if e.kind in ("MISMATCH", "TIMEOUT")]


@dataclass(frozen=True, slots=True)
class Debrief:
    """Готовый разбор и то, чем он составлен."""

    text: str
    engine: str
    """template — по шаблону, llm — языковой моделью."""

    fallback_reason: str | None = None
    """Почему не получилось составить моделью, если запрашивалась она."""


class Responder(Protocol):
    """Составитель текста разбора."""

    name: str

    async def compose(self, data: DebriefInput) -> str: ...


def minutes(seconds: float) -> str:
    """Длительность словами: «7 минут 20 секунд»."""
    total = int(round(seconds))
    m, s = divmod(total, 60)
    if not m:
        return f"{s} с"
    return f"{m} мин {s:02d} с"
