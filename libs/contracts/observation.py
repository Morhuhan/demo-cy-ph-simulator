"""Контракт «наблюдение» — то, что модуль компьютерного зрения шлёт в шину.

Это граница между CV-командой и сценарной частью. Принцип разделения:

    CV описывает, что видит. Решения принимает сценарный движок.

Поэтому здесь нет ни «шаг выполнен», ни «ошибка оператора» — только состояние
наблюдаемой переменной с уверенностью. Что означает конкретное состояние в
контексте СОП, знает scenario_engine, и только он.

Словарь переменных и их допустимых значений задаётся в YAML-описании СОП
(секция `observables`), а не здесь. Строгая JSON Schema с перечислениями
генерируется из YAML командой `python -m tools.gen_schemas` — см. docs/contracts.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from .common import BBox, Envelope, utc_now


class StreamState(StrEnum):
    """Состояние видеопотока со стороны CV."""

    ONLINE = "online"
    DEGRADED = "degraded"  # поток есть, но качество распознавания упало
    LOST = "lost"  # потока нет


class Observation(Envelope):
    """Одно наблюдение состояния одной переменной на одном кадре.

    Ожидаемый темп — порядка 15 сообщений в секунду на переменную. Сглаживанием
    и гашением дребезга занимается event_processor, CV сглаживать не должен:
    сырой поток нужен, чтобы пороги можно было калибровать, не трогая CV.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        description="Идентификатор источника: 'cv-emulator', 'cv-yolo-v1' и т.п.",
        examples=["cv-emulator"],
    )
    camera_id: str = Field(description="Идентификатор камеры", examples=["chest-01"])
    post_id: str | None = Field(
        default=None,
        description="Номер поста налива, если распознан по табличке (например 'С-4')",
        examples=["С-4"],
    )

    frame_ts: datetime = Field(
        default_factory=utc_now,
        description=(
            "Время кадра, по которому сделано наблюдение. Именно от него считается "
            "задержка из п. 6.2 ТЗ, а не от момента отправки."
        ),
    )

    variable: str = Field(
        description="Наблюдаемая переменная состояния оборудования",
        examples=["pipe_position"],
    )
    value: str | None = Field(
        default=None,
        description="Распознанное значение. None — если распознать не удалось (unclear=true).",
        examples=["seated"],
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Уверенность модели в значении. При unclear=true трактуется как 0.",
    )
    unclear: bool = Field(
        default=False,
        description=(
            "Признак «не разобрал»: объект перекрыт, засветка, вне кадра. "
            "Это штатная ситуация, а не ошибка — движок отвечает вердиктом UNCLEAR."
        ),
    )

    bbox: BBox | None = Field(
        default=None, description="Где в кадре найден объект. Опционально, для отладки и разметки."
    )

    @model_validator(mode="after")
    def _check_unclear_consistency(self) -> Observation:
        if self.unclear and self.value is not None:
            raise ValueError("при unclear=true поле value должно быть null")
        if not self.unclear and self.value is None:
            raise ValueError("при unclear=false поле value обязательно")
        return self


class StreamHealth(Envelope):
    """Сообщение о состоянии видеопотока.

    Обязанность CV — сообщать о своих отказах явно, а не молча замолкать.
    Молчание источника движок тоже отследит (по сторожевому таймеру), но с
    задержкой и без причины.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    camera_id: str
    state: StreamState
    reason: str | None = Field(
        default=None,
        description="Человекочитаемая причина",
        examples=["обрыв RTSP-соединения", "засветка", "обмерзание оптики"],
    )
