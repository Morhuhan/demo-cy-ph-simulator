"""Контракты сеанса тренировки: режимы, команды инструктора, снимок состояния."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import Envelope
from .verdict import VerdictKind


class Mode(StrEnum):
    """Три режима по п. 5.1–5.3 ТЗ."""

    TRAINING = "training"  # учебный: инструкции, подтверждения, подсказки
    TESTING = "testing"  # тестовый: молча, голос только на ошибках
    DEMO = "demo"  # демонстрационный: озвучивается каждый контрольный шаг


class SessionState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"  # сценарий пройден до конца
    ABORTED = "aborted"  # остановлен инструктором или критической блокировкой


class CommandName(StrEnum):
    START = "start"
    STOP = "stop"
    RESET = "reset"
    PAUSE = "pause"
    RESUME = "resume"


class SessionCommand(Envelope):
    """Команда от интерфейса инструктора к сценарному движку."""

    model_config = ConfigDict(extra="forbid")

    command: CommandName
    mode: Mode | None = Field(default=None, description="Обязателен для START")
    trainee_name: str | None = Field(default=None, description="ФИО обучаемого, для отчёта п. 5.4")
    sop_id: str | None = Field(default=None, description="Какой СОП запускать; по умолчанию единственный")


class StepStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"  # выполнен с нарушением
    SKIPPED = "skipped"


class StepView(BaseModel):
    """Состояние одного шага для отображения инструктору."""

    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    title: str
    critical: bool = False
    status: StepStatus = StepStatus.PENDING
    last_verdict: VerdictKind | None = None
    started_ts: datetime | None = None
    finished_ts: datetime | None = None
    deadline_ts: datetime | None = Field(
        default=None, description="Когда сработает сторожевой таймер шага"
    )


class SessionSnapshot(Envelope):
    """Полное состояние сеанса.

    Публикуется на каждое изменение. UI не хранит собственного состояния и
    полностью перерисовывается по снимку — так интерфейс не может разойтись
    с движком, даже если пропустит сообщение.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    sop_id: str
    sop_title: str
    scenario_code: str = Field(
        default="",
        description="Код сценария занятия. Едет со снимком: сценарий переключается на ходу.",
    )
    mode: Mode
    state: SessionState
    trainee_name: str | None = None

    current_step_id: str | None = None
    current_step_index: int = 0
    steps: list[StepView] = Field(default_factory=list)

    errors_count: int = 0
    failed_step_ids: list[str] = Field(default_factory=list)
    blocked_message: str | None = Field(
        default=None,
        description=(
            "Текст действующей критической блокировки. Заполнен, пока нарушение "
            "не исправлено: сценарий не продвигается, пока здесь что-то есть."
        ),
    )

    observed_state: dict[str, str | None] = Field(
        default_factory=dict,
        description="Текущее подтверждённое состояние оборудования, как его видит движок",
    )
    stream_online: bool = True

    started_ts: datetime | None = None
    finished_ts: datetime | None = None
