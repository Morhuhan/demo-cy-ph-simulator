"""Контракт «вердикт» — решение сценарного движка о действии оператора.

Четыре уровня вместо бинарного «ок / не ок» (п. 1.4 вопросника Заказчика).
Разница между MISMATCH и UNCLEAR существенна на приёмке: первое — ошибка
оператора, второе — система не разобрала. Смешивать их нельзя, иначе метрика
точности из п. 6.1.3 ТЗ становится бессмысленной.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from .common import Envelope


class VerdictKind(StrEnum):
    MATCH = "MATCH"  # действие соответствует ожидаемому шагу
    MISMATCH = "MISMATCH"  # действие противоречит СОП
    UNCLEAR = "UNCLEAR"  # система не смогла разобрать происходящее
    TIMEOUT = "TIMEOUT"  # шаг не выполнен за отведённое время


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"  # нарушение блокировки, сценарий останавливается


class ReasonCode(StrEnum):
    """Машиночитаемая причина вердикта.

    Отделена от `message`, потому что текст меняется (в том числе если реплики
    начнёт формулировать LLM), а отчётность и метрики должны опираться
    на стабильный код.
    """

    STEP_COMPLETED = "step_completed"
    PRECONDITION_VIOLATED = "precondition_violated"  # шаг сделан до обязательного предыдущего
    OUT_OF_ORDER = "out_of_order"  # шаг сделан не в свою очередь, но без блокировок
    UNEXPECTED_STATE = "unexpected_state"  # состояние изменилось вопреки сценарию (откат)
    LOW_CONFIDENCE = "low_confidence"
    STREAM_LOST = "stream_lost"
    STEP_TIMEOUT = "step_timeout"


class Verdict(Envelope):
    """Оценка одного наблюдаемого действия относительно эталонной последовательности."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    step_id: str = Field(description="Шаг СОП, к которому относится вердикт")
    step_index: int = Field(ge=0, description="Порядковый номер шага, 0 — вне шагов")

    kind: VerdictKind
    severity: Severity
    reason_code: ReasonCode
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Уверенность в вердикте. Наследуется от подтверждённого состояния.",
    )

    message: str = Field(description="Человекочитаемая формулировка для журнала инструктора")

    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Контекст: какая переменная, какое значение, какие предусловия нарушены",
    )

    source_event_id: str | None = Field(
        default=None,
        description="event_id состояния, вызвавшего вердикт. Сквозная трассировка до кадра.",
    )
    latency_ms: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Задержка от frame_ts исходного кадра до вердикта. Это измеряемая часть "
            "бюджета из п. 6.2 ТЗ, приходящаяся на сценарную часть."
        ),
    )
