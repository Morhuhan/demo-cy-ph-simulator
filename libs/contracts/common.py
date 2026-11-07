"""Общие примитивы контрактов.

Версия схемы поднимается при любом несовместимом изменении моделей. Потребители
обязаны проверять `schema_version` и отбрасывать сообщения с мажорной версией,
которую не понимают, — см. docs/contracts.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


def utc_now() -> datetime:
    """Единственный источник времени в системе — UTC, tz-aware.

    Наивные datetime запрещены: события приходят из разных процессов, а на
    приёмке по ним считается задержка из п. 6.2 ТЗ.
    """
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Envelope(BaseModel):
    """База для всего, что летает по шине."""

    model_config = ConfigDict(extra="forbid", ser_json_timedelta="float")

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Версия контракта. Мажорная часть — признак совместимости.",
    )
    event_id: str = Field(default_factory=new_id, description="Уникальный идентификатор сообщения")
    ts: datetime = Field(default_factory=utc_now, description="Момент формирования сообщения, UTC")


class BBox(BaseModel):
    """Ограничивающий прямоугольник в нормализованных координатах кадра [0..1]."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
