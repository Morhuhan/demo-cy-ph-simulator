"""Контракт CV-модуля «как он есть на проводе».

Это зеркало моделей из `src/schemas.py` репозитория CV-модуля, а не наш
внутренний контракт. Держать его отдельно принципиально: расхождения между
тем, что присылает смежник, и тем, с чем работает движок, должны быть видны
в одном месте и превращаться в ошибку валидации, а не растекаться по коду.

Разбор расхождений и список на согласование — docs/integration-cv.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ObservationStatus(StrEnum):
    """Статус наблюдения по техническому описанию CV-модуля от 08.09.2026."""

    RECOGNIZED = "RECOGNIZED"  # объект виден, состояние определено
    UNCLEAR = "UNCLEAR"  # объект виден, состояние определить нельзя
    NOT_VISIBLE = "NOT_VISIBLE"  # объекта нет в кадре
    NO_DATA = "NO_DATA"  # данных для наблюдения нет


class ServiceStatus(StrEnum):
    NO_DATA = "NO_DATA"
    AVAILABLE = "AVAILABLE"


class WireEvent(BaseModel):
    # extra="ignore", а не "forbid": добавленное смежником поле не должно ронять
    # приём. Отсутствие ожидаемого поля — ошибка, появление лишнего — нет.
    #
    # protected_namespaces снят: поле model_version названо так в контракте
    # смежника, и переименовать его мы не можем.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    camera_id: str = Field(min_length=1)
    timestamp: datetime

    @field_validator("timestamp", "frame_ts", check_fields=False)
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("время должно содержать часовой пояс")
        return value.astimezone(UTC)


class ObjectStateEvent(WireEvent):
    """Подтверждённое изменение состояния объекта.

    CV присылает такие события не покадрово, а только при смене состояния —
    стабилизация выполнена на его стороне.
    """

    event_type: Literal["object_state"]

    object: str = Field(min_length=1, description="Что наблюдается: 'helmet' и т.п.")
    subject_id: str | None = Field(
        default=None,
        description="К кому относится состояние. Идентификатор трека, не человека.",
        examples=["person-1"],
    )
    state: str = Field(min_length=1, examples=["on", "off"])
    confidence: float = Field(ge=0, le=1)
    observation_status: ObservationStatus
    model_version: str = Field(min_length=1)
    bbox: tuple[float, float, float, float] | None = None

    frame_ts: datetime | None = Field(
        default=None,
        description="Время кадра меткой времени, если источник шлёт его так.",
    )
    frame_width: int | None = Field(
        default=None,
        gt=0,
        description="Ширина кадра в пикселях. По ней пиксельная рамка приводится к долям кадра.",
    )
    frame_height: int | None = Field(
        default=None,
        gt=0,
        description="Высота кадра в пикселях.",
    )

    frame_timestamp_sec: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Положение кадра от начала съёмки, в секундах. Именно так время "
            "кадра передаёт CV-модуль. Смещение, а не метка времени: у записи "
            "нет общего с нами календаря, есть только собственный отсчёт."
        ),
    )


class ServiceEvent(WireEvent):
    """Служебное сообщение CV о собственном состоянии."""

    event_type: Literal["cv_service"]

    status: ServiceStatus
    reason: str = Field(examples=["stream_lost", "stream_restored"])
