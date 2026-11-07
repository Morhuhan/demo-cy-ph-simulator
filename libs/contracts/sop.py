"""Схема машиночитаемого СОП.

Регламент живёт в YAML, а не в Python (п. 1.3 вопросника Заказчика). Цель —
чтобы добавление шага, смена порогов или правка формулировок не требовали
изменений в коде, и чтобы будущий автопарсер PDF мог генерировать такие файлы.

Здесь же объявляется словарь наблюдаемых переменных, из которого генерируется
строгая JSON Schema для CV-команды. Один источник правды: изменил YAML —
перегенерировал контракт.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .speech import VoiceIntent
from .verdict import Severity


class ObservableValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Как называть значение в журнале и в речи")


class Observable(BaseModel):
    """Наблюдаемая переменная состояния оборудования — единица работы CV-модуля."""

    model_config = ConfigDict(extra="forbid")

    title: str
    values: dict[str, ObservableValue] = Field(min_length=2)
    initial: str = Field(description="Состояние в начале сценария")
    description: str | None = Field(
        default=None, description="Пояснение для CV-команды: что именно смотреть в кадре"
    )

    @model_validator(mode="after")
    def _initial_is_known(self) -> Observable:
        if self.initial not in self.values:
            raise ValueError(
                f"initial='{self.initial}' отсутствует среди values: {sorted(self.values)}"
            )
        return self


class Expectation(BaseModel):
    """Условие завершения шага.

    Ровно одна форма из двух: либо состояние оборудования, либо голосовая
    команда оператора (седьмой шаг СОП камерой не наблюдается).
    """

    model_config = ConfigDict(extra="forbid")

    variable: str | None = None
    equals: str | None = None
    voice_intent: VoiceIntent | None = None

    @model_validator(mode="after")
    def _exactly_one_form(self) -> Expectation:
        state_form = self.variable is not None
        voice_form = self.voice_intent is not None
        if state_form == voice_form:
            raise ValueError("укажите либо variable+equals, либо voice_intent, но не оба сразу")
        if state_form and self.equals is None:
            raise ValueError("для variable обязательно поле equals")
        return self

    def describe(self) -> str:
        if self.voice_intent is not None:
            return f"голосовая команда '{self.voice_intent}'"
        return f"{self.variable}={self.equals}"


class StepPhrases(BaseModel):
    """Реплики ассистента вокруг шага.

    Постоянные фразы кэшируются и прогреваются при старте, поэтому держать их
    здесь, а не собирать в рантайме, — ещё и вопрос задержки.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(description="Что делать. Звучит в учебном и демо-режимах.")
    success: str = Field(description="Подтверждение выполнения")
    violation: str | None = Field(
        default=None,
        description="Что сказать, если шаг сделан с нарушением предусловий",
    )
    hint: str | None = Field(default=None, description="Ответ на команду «помощь»")
    timeout: str | None = Field(default=None, description="Что сказать по сторожевому таймеру")


class SopStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(description="Формулировка шага из регламента")

    expect: list[Expectation] = Field(min_length=1, description="Все условия должны выполниться")
    requires: list[str] = Field(
        default_factory=list,
        description=(
            "Шаги, без которых этот выполнять нельзя. Нарушение — критическая "
            "блокировка, а не просто нарушение порядка."
        ),
    )
    critical: bool = Field(
        default=False, description="Шаг с явными последствиями ошибки по регламенту"
    )

    timeout_sec: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Сторожевой таймер шага. Задаётся индивидуально: шаг 6 — минуты кручения "
            "лебёдки, шаг 2 — секунды. Общий таймаут давал бы ложные TIMEOUT."
        ),
    )
    min_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Порог уверенности для зачёта шага; ниже — вердикт UNCLEAR",
    )

    accept_current_state: bool = Field(
        default=False,
        description=(
            "Шаг проверяет состояние, а не действие: условие, уже выполненное на "
            "старте занятия, засчитывается сразу. Проверка каски проходит, даже "
            "если сотрудник встал перед камерой уже в ней. Для шагов-действий "
            "(подключить заземление) не годится: там важно само действие."
        ),
    )

    consequence: str | None = Field(
        default=None,
        description="Последствие ошибки по регламенту. Идёт в голос и в отчёт.",
    )
    phrases: StepPhrases


class Invariant(BaseModel):
    """Условие, которое должно соблюдаться после определённого шага и до конца сценария.

    Пример: заземление, подключённое на шаге 1, не должно пропасть на шаге 5.
    Без этого движок принял бы «лампа погасла» просто за смену состояния.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    after_step: str = Field(description="С какого шага условие начинает действовать")
    variable: str
    must_equal: str
    severity: Severity = Severity.CRITICAL
    message: str


class ScenarioPhrases(BaseModel):
    model_config = ConfigDict(extra="forbid")

    welcome: str
    finished: str
    aborted: str
    unclear: str = Field(description="Когда система не разобрала происходящее")
    stream_lost: str = Field(description="Когда пропал видеопоток")
    help_fallback: str = Field(description="Ответ на «помощь», если у шага нет своей подсказки")


class SopSpec(BaseModel):
    """Полное машиночитаемое описание сценария тренировки."""

    model_config = ConfigDict(extra="forbid")

    id: str
    scenario_code: str = Field(description="Код сценария по п. 4.1 ТЗ", examples=["КФТ-01"])
    title: str
    description: str | None = None
    source_document: str | None = Field(
        default=None, description="Регламент-источник, для прослеживаемости на приёмке"
    )

    observables: dict[str, Observable] = Field(min_length=1)
    steps: list[SopStep] = Field(min_length=1)
    invariants: list[Invariant] = Field(default_factory=list)
    phrases: ScenarioPhrases

    @model_validator(mode="after")
    def _validate_references(self) -> SopSpec:
        step_index = {step.id: i for i, step in enumerate(self.steps)}
        if len(step_index) != len(self.steps):
            raise ValueError("идентификаторы шагов должны быть уникальны")

        for position, step in enumerate(self.steps):
            for expectation in step.expect:
                if expectation.variable is None:
                    continue
                observable = self.observables.get(expectation.variable)
                if observable is None:
                    raise ValueError(
                        f"шаг '{step.id}': неизвестная переменная '{expectation.variable}'"
                    )
                if expectation.equals not in observable.values:
                    raise ValueError(
                        f"шаг '{step.id}': значение '{expectation.equals}' недопустимо для "
                        f"'{expectation.variable}', ожидалось одно из {sorted(observable.values)}"
                    )

            for required in step.requires:
                if required not in step_index:
                    raise ValueError(f"шаг '{step.id}': requires ссылается на неизвестный '{required}'")
                if step_index[required] >= position:
                    raise ValueError(
                        f"шаг '{step.id}': requires ссылается на '{required}', который идёт "
                        "не раньше по сценарию"
                    )

        for invariant in self.invariants:
            if invariant.after_step not in step_index:
                raise ValueError(
                    f"инвариант '{invariant.id}': неизвестный шаг '{invariant.after_step}'"
                )
            observable = self.observables.get(invariant.variable)
            if observable is None:
                raise ValueError(
                    f"инвариант '{invariant.id}': неизвестная переменная '{invariant.variable}'"
                )
            if invariant.must_equal not in observable.values:
                raise ValueError(
                    f"инвариант '{invariant.id}': значение '{invariant.must_equal}' недопустимо"
                )

        return self

    def step_position(self, step_id: str) -> int:
        for i, step in enumerate(self.steps):
            if step.id == step_id:
                return i
        raise KeyError(step_id)

    def initial_state(self) -> dict[str, str]:
        return {name: obs.initial for name, obs in self.observables.items()}
