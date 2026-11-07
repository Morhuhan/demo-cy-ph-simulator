"""Контракт «подтверждённое состояние» — выход сглаживания, вход сценарного движка.

Разница с Observation принципиальная. Observation — это «на кадре 14:23:07.412
труба похожа на seated с уверенностью 0.61». StateChange — это «труба перешла
в состояние seated, подтверждено окном в 1.5 с из 22 наблюдений».

Движок работает только с подтверждёнными состояниями. Если бы он читал сырой
поток, труба на полпути металась бы между двумя значениями и он выдавал бы
десятки вердиктов на один физический шаг.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field

from .common import Envelope


class StateQuality(StrEnum):
    CONFIRMED = "confirmed"  # состояние устоялось, можно принимать решение
    UNCLEAR = "unclear"  # наблюдения есть, но согласия/уверенности не хватает
    STALE = "stale"  # наблюдений по переменной давно не было


class StateChange(Envelope):
    """Смена подтверждённого состояния переменной.

    Публикуется только при изменении, а не на каждое наблюдение.
    """

    model_config = ConfigDict(extra="forbid")

    variable: str
    value: str | None = Field(description="None при quality != confirmed")
    previous_value: str | None = Field(default=None, description="Что было до перехода")

    quality: StateQuality
    confidence: float = Field(
        ge=0.0, le=1.0, description="Средневзвешенная уверенность по окну подтверждения"
    )
    agreement: float = Field(
        ge=0.0,
        le=1.0,
        description="Доля наблюдений в окне, согласных с принятым значением",
    )

    first_seen_ts: datetime = Field(
        description="frame_ts первого наблюдения, поддержавшего этот переход"
    )
    confirmed_ts: datetime = Field(
        description=(
            "Момент, когда сглаживание признало состояние устоявшимся. "
            "Задержка сглаживания = confirmed_ts - first_seen_ts."
        )
    )
    observations: int = Field(ge=0, description="Сколько наблюдений вошло в окно")

    source: str
    camera_id: str
    post_id: str | None = None
