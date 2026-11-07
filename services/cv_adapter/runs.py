"""Схема скриптованного прогона эмулятора.

Прогон — это воспроизводимая запись того, что «происходит на полигоне»:
когда меняется состояние оборудования, насколько грязно это видит CV, когда
рвётся поток. Один и тот же прогон даёт один и тот же результат, поэтому по
нему можно мерить задержку и калибровать пороги.

Секция `expected` превращает прогон в приёмочный тест: ожидаемая
последовательность вердиктов проверяется автоматически (см. tests/), что и
требует п. 5 вопросника Заказчика — приёмка по интерфейсам, а не по экрану.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from libs.contracts import ReasonCode, StreamState, VerdictKind, VoiceIntent


class NoiseProfile(BaseModel):
    """Насколько грязно эмулируемый CV видит сцену.

    Значения по умолчанию соответствуют «хорошей» работе модели. Чтобы
    проверить поведение системы на плохих данных, профиль ухудшается в самом
    прогоне, а пороги сглаживания при этом не трогаются.
    """

    model_config = ConfigDict(extra="forbid")

    confidence_mean: float = Field(default=0.88, ge=0, le=1)
    confidence_jitter: float = Field(default=0.06, ge=0, le=1)
    flicker_prob: float = Field(
        default=0.02,
        ge=0,
        le=1,
        description="Вероятность выдать соседнее значение вместо истинного",
    )
    unclear_prob: float = Field(
        default=0.01, ge=0, le=1, description="Вероятность кадра «не разобрал»"
    )


class FlickerSpec(BaseModel):
    """Устойчивый дребезг переменной между двумя значениями.

    Главный тест сглаживания: труба на полпути между over_hatch и seated
    мечется десятки раз в секунду, а система обязана стоять спокойно.
    """

    model_config = ConfigDict(extra="forbid")

    variable: str
    between: list[str] = Field(min_length=2, max_length=2)
    duration_sec: float = Field(gt=0)
    confidence_mean: float = Field(default=0.5, ge=0, le=1)


class TimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: float = Field(ge=0, description="Секунда от начала прогона")
    note: str | None = Field(default=None, description="Комментарий, попадает в журнал стенда")

    set: dict[str, str] | None = Field(
        default=None, description="Новое истинное состояние оборудования"
    )
    transition_sec: float = Field(
        default=0.8,
        ge=0,
        description=(
            "Длительность физического перехода. Внутри окна CV видит то старое, "
            "то новое значение — так ведёт себя реальная модель."
        ),
    )

    flicker: FlickerSpec | None = None

    stream: StreamState | None = Field(default=None, description="Смена состояния видеопотока")
    stream_reason: str | None = None

    voice: VoiceIntent | None = Field(
        default=None, description="Эмуляция реплики оператора (седьмой шаг СОП)"
    )

    noise: NoiseProfile | None = Field(
        default=None, description="Сменить профиль шума начиная с этого момента"
    )

    @model_validator(mode="after")
    def _not_empty(self) -> TimelineEvent:
        # Событие только с note законно: это отметка на таймлайне, она попадает
        # в журнал стенда и помогает читать прогон при разборе.
        if not any((self.set, self.flicker, self.stream, self.voice, self.noise, self.note)):
            raise ValueError("событие таймлайна ничего не делает")
        return self


class ExpectedVerdict(BaseModel):
    """Ожидаемый вердикт — строка приёмочного критерия."""

    model_config = ConfigDict(extra="forbid")

    step: str
    kind: VerdictKind
    reason: ReasonCode | None = None


class EmulatorRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    scenario: str | None = Field(
        default=None,
        description=(
            "Идентификатор СОП, к которому относится прогон. Прогоны одного "
            "сценария оперируют его переменными и под другим СОП бессмысленны. "
            "None — прогон не привязан к сценарию и подходит любому."
        ),
        examples=["kft-01"],
    )
    description: str | None = None
    duration_sec: float = Field(gt=0, description="Когда остановить прогон")
    noise: NoiseProfile = Field(default_factory=NoiseProfile)
    timeline: list[TimelineEvent] = Field(min_length=1)
    expected: list[ExpectedVerdict] = Field(
        default_factory=list,
        description="Ожидаемая последовательность вердиктов. Пусто — прогон без проверки.",
    )

    @model_validator(mode="after")
    def _sorted_timeline(self) -> EmulatorRun:
        self.timeline.sort(key=lambda event: event.at)
        return self


def load_run(path: str | Path) -> EmulatorRun:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return EmulatorRun.model_validate(data)


def load_runs(directory: str | Path, *, scenario: str | None = None) -> dict[str, EmulatorRun]:
    """Прогоны из каталога, ключ — идентификатор прогона.

    При заданном `scenario` берутся только прогоны этого сценария и прогоны без
    привязки. Прогон чужого сценария оперирует переменными, которых нет в
    текущем СОП, и упал бы на первом же `set` — отфильтровать его на загрузке
    честнее, чем уронить стенд на демонстрации.
    """
    runs: dict[str, EmulatorRun] = {}
    for file in sorted(Path(directory).glob("*.yaml")):
        run = load_run(file)
        if scenario is not None and run.scenario is not None and run.scenario != scenario:
            continue
        runs[run.id] = run
    return runs
