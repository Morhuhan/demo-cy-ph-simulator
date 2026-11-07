"""Сценарный движок — сердце системы.

Детерминированный автомат над машиночитаемым СОП. Никаких вероятностных
моделей в тракте принятия решения: шаг 1 блокирует шаг 2 потому, что иначе
искра от статического электричества, и такое решение обязано быть
воспроизводимым, объяснимым и быстрым.

Класс намеренно не знает ни про шину, ни про asyncio — на вход состояния и
команды, на выход вердикты и реплики. Благодаря этому весь сценарий целиком
прогоняется в тестах за доли секунды.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from libs.config import EngineConfig
from libs.contracts import (
    Mode,
    Priority,
    ReasonCode,
    SessionSnapshot,
    SessionState,
    Severity,
    SopSpec,
    SopStep,
    SpeakRequest,
    StateChange,
    StateQuality,
    StepStatus,
    StepView,
    UtteranceKind,
    Verdict,
    VerdictKind,
    VoiceCommand,
    VoiceIntent,
    new_id,
    utc_now,
)

log = logging.getLogger(__name__)


@dataclass
class EngineOutput:
    """Что движок произвёл в ответ на одно входное событие."""

    verdicts: list[Verdict] = field(default_factory=list)
    speech: list[SpeakRequest] = field(default_factory=list)
    snapshot: SessionSnapshot | None = None

    def extend(self, other: EngineOutput) -> None:
        self.verdicts.extend(other.verdicts)
        self.speech.extend(other.speech)
        if other.snapshot is not None:
            self.snapshot = other.snapshot


#: Что озвучивается в каждом режиме (п. 5.1–5.3 ТЗ).
_SPEECH_POLICY: dict[Mode, frozenset[UtteranceKind]] = {
    # Учебный: максимум поддержки.
    Mode.TRAINING: frozenset(UtteranceKind),
    # Тестовый: самостоятельная работа. Голос только там, где по ТЗ обязан
    # вмешаться — ошибка и подсказка по прямому запросу.
    Mode.TESTING: frozenset(
        {UtteranceKind.WARNING, UtteranceKind.CRITICAL, UtteranceKind.HINT, UtteranceKind.REPORT}
    ),
    # Демонстрационный: озвучивается каждый контрольный шаг.
    Mode.DEMO: frozenset(UtteranceKind),
}


class ScenarioEngine:
    def __init__(self, sop: SopSpec, config: EngineConfig) -> None:
        self._sop = sop
        self._config = config

        self.session_id: str = new_id()
        self.mode: Mode = Mode(config.default_mode)
        self.state: SessionState = SessionState.IDLE
        self.trainee_name: str | None = None

        self._completed: set[str] = set()
        self._status: dict[str, StepStatus] = {}
        self._verdict_of: dict[str, VerdictKind] = {}
        self._started_step_at: dict[str, datetime] = {}
        self._finished_step_at: dict[str, datetime] = {}
        self._current: int = 0

        self._observed: dict[str, str | None] = dict(sop.initial_state())
        # Уверенность хранится по переменным, а не общая: порог шага должен
        # сверяться с уверенностью в том состоянии, которое этот шаг описывает,
        # а не в том, что случайно пришло последним.
        self._confidence: dict[str, float] = {}
        self._rejected: dict[str, tuple[str, ...]] = {}
        self._low_confidence_since: dict[str, datetime] = {}
        self._stream_online: bool = True
        #: С какого момента источник молчит. Пока система слепа, сторожевой
        #: таймер шага стоит: обучаемый не отвечает за отказ камеры.
        self._blind_since: datetime | None = None

        self._errors: int = 0
        self._failed: list[str] = []
        self._blocked_message: str | None = None

        self._started_ts: datetime | None = None
        self._finished_ts: datetime | None = None
        self._deadline: datetime | None = None
        #: Когда занятие поставили на паузу. На это время сдвигаются сроки
        #: шагов: пауза не должна съедать отведённое обучаемому время.
        self._paused_at: datetime | None = None
        self._timeout_fired: set[str] = set()
        self._last_unclear_at: datetime | None = None
        self._last_instruction: SpeakRequest | None = None

        self._reset_steps()

    # ------------------------------------------------------------------
    # Управление сеансом
    # ------------------------------------------------------------------

    def start(
        self,
        mode: Mode | None = None,
        trainee_name: str | None = None,
        *,
        now: datetime | None = None,
    ) -> EngineOutput:
        now = now or utc_now()
        self.session_id = new_id()
        self.mode = mode or self.mode
        self.trainee_name = trainee_name
        self.state = SessionState.RUNNING
        self._reset_steps()
        self._errors = 0
        self._failed = []
        self._blocked_message = None
        self._timeout_fired.clear()
        self._started_ts = now
        self._finished_ts = None
        self._current = 0

        log.info("сеанс %s: старт, режим %s", self.session_id, self.mode)

        output = EngineOutput()
        self._say(
            output,
            self._sop.phrases.welcome,
            UtteranceKind.INSTRUCTION,
            cache_key="scenario.welcome",
        )
        self._enter_step(output, now)
        # Шаги-проверки состояния засчитываются по тому, что уже видно: иначе
        # сотрудник, вставший перед камерой в каске, не прошёл бы проверку, пока
        # не снял бы её и не надел снова — смены состояния для движка нет.
        state_checks = {step.id for step in self._sop.steps if step.accept_current_state}
        if state_checks:
            self._catch_up(output, now, limit_to=state_checks)
        output.snapshot = self.snapshot()
        return output

    def stop(self, *, now: datetime | None = None) -> EngineOutput:
        now = now or utc_now()
        output = EngineOutput()
        if self.state in (SessionState.RUNNING, SessionState.PAUSED):
            self.state = SessionState.ABORTED
            self._finished_ts = now
            self._deadline = None
            self._paused_at = None
            self._say(
                output,
                self._sop.phrases.aborted,
                UtteranceKind.REPORT,
                priority=Priority.INTERRUPT,
                cache_key="scenario.aborted",
            )
            log.info("сеанс %s: остановлен инструктором", self.session_id)
        output.snapshot = self.snapshot()
        return output

    def pause(self, *, now: datetime | None = None) -> EngineOutput:
        """Приостановить занятие.

        Движок и так не выносит вердиктов вне RUNNING, поэтому достаточно
        сменить состояние и запомнить момент. Сторожевой таймер шага замирает
        вместе с занятием — иначе пауза засчиталась бы обучаемому как
        потраченное время.
        """
        now = now or utc_now()
        if self.state is SessionState.RUNNING:
            self.state = SessionState.PAUSED
            self._paused_at = now
            log.info("сеанс %s: пауза", self.session_id)
        return EngineOutput(snapshot=self.snapshot())

    def resume(self, *, now: datetime | None = None) -> EngineOutput:
        """Продолжить занятие и вернуть обучаемому время, потраченное на паузу."""
        now = now or utc_now()
        output = EngineOutput()
        if self.state is not SessionState.PAUSED:
            output.snapshot = self.snapshot()
            return output

        if self._paused_at is not None:
            shift = now - self._paused_at
            if self._deadline is not None:
                self._deadline += shift
            # Начало шага сдвигается вместе со сроком: иначе полоса таймера
            # на пульте дёрнулась бы, посчитав паузу частью шага.
            step = self.current_step()
            if step is not None and step.id in self._started_step_at:
                self._started_step_at[step.id] += shift
        self._paused_at = None
        if self._blind_since is not None:
            # Время паузы уже возвращено выше. Отсчёт слепоты начинается
            # заново, иначе один и тот же простой сдвинул бы срок дважды.
            self._blind_since = now
        self.state = SessionState.RUNNING

        step = self.current_step()
        if step is not None:
            self._say(
                output,
                step.phrases.instruction,
                UtteranceKind.INSTRUCTION,
                cache_key=f"step.{step.id}.instruction",
            )
        log.info("сеанс %s: продолжение", self.session_id)
        output.snapshot = self.snapshot()
        return output

    def reset(self) -> EngineOutput:
        # Новый идентификатор обязателен: иначе снимок сброшенного сеанса
        # перезаписал бы в журнале запись предыдущего — с его вердиктами,
        # длительностью и ошибками. Сброс готовит следующую тренировку,
        # а не стирает прошедшую.
        self.session_id = new_id()
        self.state = SessionState.IDLE
        self._reset_steps()
        # Наблюдаемое состояние не сбрасываем: это состояние мира, а не
        # занятия. Сглаживание о сбросе не знает и повторно смену не пришлёт,
        # поэтому возврат к начальному «каска не надета» разошёлся бы с кадром
        # до первой настоящей смены.
        self._errors = 0
        self._failed = []
        self._blocked_message = None
        self._deadline = None
        self._paused_at = None
        self._blind_since = None
        self._timeout_fired.clear()
        self._started_ts = None
        self._finished_ts = None
        self._current = 0
        return EngineOutput(snapshot=self.snapshot())

    # ------------------------------------------------------------------
    # Вход: подтверждённые состояния
    # ------------------------------------------------------------------

    def on_state_change(self, change: StateChange, *, now: datetime | None = None) -> EngineOutput:
        now = now or utc_now()
        output = EngineOutput()

        if change.quality is StateQuality.STALE:
            if self.state is SessionState.PAUSED:
                # Источник молчит потому, что занятие приостановлено.
                # Объявлять это потерей видеопотока нельзя.
                output.snapshot = self.snapshot()
                return output
            self._observed[change.variable] = None
            self._stream_online = False
            if self._blind_since is None and self.state is SessionState.RUNNING:
                self._blind_since = now
            self._emit_stream_lost(output, change, now)
            output.snapshot = self.snapshot()
            return output

        if change.quality is StateQuality.UNCLEAR:
            self._observed[change.variable] = None
            self._emit_unclear(output, change, now)
            output.snapshot = self.snapshot()
            return output

        # Подтверждённое состояние.
        self._end_blindness(now)
        self._stream_online = True
        self._observed[change.variable] = change.value
        self._confidence[change.variable] = change.confidence

        if self.state is not SessionState.RUNNING:
            # Вне сеанса состояние отслеживаем, но вердиктов не выносим:
            # оператор может готовить пост до начала тренировки.
            output.snapshot = self.snapshot()
            return output

        self._check_invariants(output, change, now)
        self._match_steps(output, change, now)

        output.snapshot = self.snapshot()
        return output

    def on_voice(self, command: VoiceCommand, *, now: datetime | None = None) -> EngineOutput:
        now = now or utc_now()
        output = EngineOutput()

        if command.intent is VoiceIntent.REPEAT:
            if self._last_instruction is not None:
                output.speech.append(
                    self._last_instruction.model_copy(
                        update={"event_id": new_id(), "priority": Priority.INTERRUPT}
                    )
                )
            return output

        if command.intent is VoiceIntent.HELP:
            step = self.current_step()
            hint = (step.phrases.hint if step else None) or self._sop.phrases.help_fallback
            self._say(output, hint, UtteranceKind.HINT, priority=Priority.INTERRUPT, force=True)
            return output

        if command.intent is VoiceIntent.READY_REPORT:
            if self.state is SessionState.RUNNING:
                self._match_steps(output, None, now, voice_intent=command.intent)
                output.snapshot = self.snapshot()
            return output

        return output

    def tick(self, now: datetime | None = None) -> EngineOutput:
        """Сторожевой таймер. Вызывается периодически снаружи."""
        now = now or utc_now()
        output = EngineOutput()

        if self.state is not SessionState.RUNNING:
            return output

        # Шаги, отложенные из-за низкой уверенности, дозревают здесь: новых
        # наблюдений о смене состояния может уже не быть.
        self._resolve_pending(output, now)
        if output.verdicts:
            output.snapshot = self.snapshot()

        if self._blind_since is not None:
            # Источник молчит. Объявить невыполнение шага сейчас значило бы
            # записать отказ камеры в нарушение обучаемого.
            return output

        if self._deadline is None or now < self._deadline:
            return output

        step = self.current_step()
        if step is None or step.id in self._timeout_fired:
            self._deadline = None
            return output

        # Срабатывает один раз на шаг: повторять одно и то же каждые N секунд
        # значит забить журнал и заглушить действительно важные реплики.
        self._timeout_fired.add(step.id)
        self._deadline = None

        verdict = self._verdict(
            step,
            VerdictKind.TIMEOUT,
            Severity.WARNING,
            ReasonCode.STEP_TIMEOUT,
            confidence=1.0,
            message=f"Шаг «{step.title}» не выполнен за {step.timeout_sec:.0f} с",
            details={"timeout_sec": step.timeout_sec},
            now=now,
        )
        output.verdicts.append(verdict)
        self._verdict_of[step.id] = VerdictKind.TIMEOUT

        phrase = step.phrases.timeout or step.phrases.instruction
        self._say(output, phrase, UtteranceKind.WARNING, verdict_id=verdict.event_id)
        output.snapshot = self.snapshot()
        return output

    # ------------------------------------------------------------------
    # Ядро сверки
    # ------------------------------------------------------------------

    def _match_steps(
        self,
        output: EngineOutput,
        change: StateChange | None,
        now: datetime,
        *,
        voice_intent: VoiceIntent | None = None,
    ) -> None:
        """Оценить действие оператора.

        Вердикт выносится на действие, а не на состояние мира вообще. Поэтому
        рассматриваются только шаги, которых это конкретное изменение касается:
        иначе смена положения трубы заставляла бы движок заново переоценивать
        механизм прижатия и сыпать повторами.
        """
        step = self._triggered_step(change, voice_intent)
        if step is None:
            return

        missing = self._unmet_requirements(step)
        if missing:
            # Прежде чем обвинять оператора, убеждаемся, что пропущенные шаги
            # действительно не выполнены. Если условия предыдущего шага по
            # факту соблюдены, а формально он не закрыт (не хватило уверенности
            # в момент перехода), — это наша неуверенность, а не его нарушение.
            self._catch_up(output, now, limit_to=set(missing))
            missing = self._unmet_requirements(step)

        if missing:
            self._reject(output, step, missing, change, now)
            return

        confidence = self._step_confidence(step, change)
        if not self._confidence_settled(step, confidence, now):
            return

        self._accept(output, step, change, now, confidence=confidence)
        # Закрытие шага могло разблокировать следующие: например, прижатие
        # засчитано с опозданием, а труба к этому моменту уже опущена до дна.
        self._catch_up(output, now)

    def _triggered_step(
        self, change: StateChange | None, voice_intent: VoiceIntent | None
    ) -> SopStep | None:
        """Первый незакрытый шаг, условия которого выполнены именно этим событием."""
        for step in self._sop.steps:
            if step.id in self._completed:
                continue
            if not self._is_triggered_by(step, change, voice_intent):
                continue
            if self._expectations_met(step, voice_intent=voice_intent):
                return step
        return None

    def _is_triggered_by(
        self, step: SopStep, change: StateChange | None, voice_intent: VoiceIntent | None
    ) -> bool:
        for expectation in step.expect:
            if expectation.voice_intent is not None:
                if voice_intent is not None and expectation.voice_intent == voice_intent:
                    return True
                continue
            if change is not None and expectation.variable == change.variable:
                return True
        return False

    def _catch_up(
        self, output: EngineOutput, now: datetime, *, limit_to: set[str] | None = None
    ) -> None:
        """Закрыть шаги, условия которых уже соблюдены в наблюдаемом состоянии.

        Порог уверенности здесь не применяется намеренно: состояние мира само
        по себе является подтверждением. Шаг с голосовым условием так закрыть
        нельзя — доклад относится к моменту, а не к состоянию.
        """
        for _ in range(len(self._sop.steps)):
            accepted = False
            for step in self._sop.steps:
                if step.id in self._completed:
                    continue
                if limit_to is not None and step.id not in limit_to:
                    continue
                if any(expectation.voice_intent is not None for expectation in step.expect):
                    continue
                if not self._expectations_met(step, voice_intent=None):
                    continue
                if self._unmet_requirements(step):
                    continue
                self._accept(
                    output, step, None, now, confidence=self._step_confidence(step, None)
                )
                accepted = True
                break
            if not accepted:
                return

    def _unmet_requirements(self, step: SopStep) -> list[str]:
        return [required for required in step.requires if required not in self._completed]

    def _step_confidence(self, step: SopStep, change: StateChange | None) -> float:
        """Уверенность в шаге — минимум по всем состояниям, которые он описывает."""
        values: list[float] = []
        for expectation in step.expect:
            if expectation.variable is None:
                continue
            if change is not None and change.variable == expectation.variable:
                values.append(change.confidence)
            else:
                values.append(self._confidence.get(expectation.variable, 1.0))
        return min(values, default=1.0)

    def _expectations_met(self, step: SopStep, *, voice_intent: VoiceIntent | None) -> bool:
        for expectation in step.expect:
            if expectation.voice_intent is not None:
                # Голосовое условие выполняется только в момент самой реплики,
                # накапливать его нельзя — доклад относится к текущему моменту.
                if voice_intent != expectation.voice_intent:
                    return False
                continue
            if self._observed.get(expectation.variable) != expectation.equals:
                return False
        return True

    def _accept(
        self,
        output: EngineOutput,
        step: SopStep,
        change: StateChange | None,
        now: datetime,
        *,
        confidence: float = 1.0,
    ) -> None:
        self._completed.add(step.id)
        self._status[step.id] = StepStatus.DONE
        self._verdict_of[step.id] = VerdictKind.MATCH
        self._finished_step_at[step.id] = now
        self._blocked_message = None
        self._rejected.pop(step.id, None)

        details = self._details(step, change)
        if step.min_confidence is not None and confidence < step.min_confidence:
            # Шаг засчитан, но уверенность ниже порога. Это не ошибка оператора
            # и не отказ распознавания — это оговорка системы, и на разборе
            # тренировки она должна быть видна.
            details["low_confidence"] = True
            details["min_confidence"] = step.min_confidence

        verdict = self._verdict(
            step,
            VerdictKind.MATCH,
            Severity.INFO,
            ReasonCode.STEP_COMPLETED,
            confidence=confidence,
            message=f"Шаг «{step.title}» выполнен",
            details=details,
            now=now,
            change=change,
        )
        output.verdicts.append(verdict)
        self._say(
            output,
            step.phrases.success,
            UtteranceKind.CONFIRMATION,
            verdict_id=verdict.event_id,
            cache_key=f"step.{step.id}.success",
            source_frame_ts=change.first_seen_ts if change else None,
        )

        position = self._sop.step_position(step.id)
        if position == self._current:
            self._current = position + 1
        else:
            # Шаг закрыт не по очереди, но с соблюдением предусловий.
            self._current = max(self._current, position + 1)

        if self._current >= len(self._sop.steps):
            self._finish(output, now)
        else:
            self._enter_step(output, now)

    def _reject(
        self,
        output: EngineOutput,
        step: SopStep,
        missing: list[str],
        change: StateChange | None,
        now: datetime,
    ) -> None:
        """Условия шага выполнены, но предусловия — нет."""
        # Повтор того же нарушения не переоценивается: пока оператор не изменил
        # ситуацию, он уже всё услышал. Иначе одно нарушение при дребезге
        # состояния превратилось бы в десяток одинаковых вердиктов.
        signature = tuple(missing)
        if self._rejected.get(step.id) == signature:
            return
        self._rejected[step.id] = signature

        # Тяжесть определяется тем, насколько критичен пропущенный шаг: пропуск
        # заземления перед открытием люка — критическая блокировка, пропуск
        # подведения трубы перед опусканием — нарушение порядка.
        blocking_critical = any(self._step_by_id(sid).critical for sid in missing) or step.critical
        severity = Severity.CRITICAL if blocking_critical else Severity.WARNING
        reason = (
            ReasonCode.PRECONDITION_VIOLATED if blocking_critical else ReasonCode.OUT_OF_ORDER
        )

        if self._status.get(step.id) is not StepStatus.FAILED:
            self._errors += 1
            if step.id not in self._failed:
                self._failed.append(step.id)
        self._status[step.id] = StepStatus.FAILED
        self._verdict_of[step.id] = VerdictKind.MISMATCH

        titles = ", ".join(f"«{self._step_by_id(sid).title}»" for sid in missing)
        verdict = self._verdict(
            step,
            VerdictKind.MISMATCH,
            severity,
            reason,
            confidence=change.confidence if change else 1.0,
            message=f"Шаг «{step.title}» выполнен без обязательного предшествующего: {titles}",
            details={
                **self._details(step, change),
                "missing_steps": missing,
                "consequence": self._step_by_id(missing[0]).consequence,
            },
            now=now,
            change=change,
        )
        output.verdicts.append(verdict)

        phrase = step.phrases.violation or self._consequence_phrase(missing)
        if severity is Severity.CRITICAL:
            self._blocked_message = phrase
            self._say(
                output,
                phrase,
                UtteranceKind.CRITICAL,
                priority=Priority.INTERRUPT,
                verdict_id=verdict.event_id,
                source_frame_ts=change.first_seen_ts if change else None,
            )
        else:
            self._say(
                output,
                phrase,
                UtteranceKind.WARNING,
                verdict_id=verdict.event_id,
                source_frame_ts=change.first_seen_ts if change else None,
            )

        log.warning(
            "сеанс %s: шаг '%s' нарушает предусловия %s (%s)",
            self.session_id,
            step.id,
            missing,
            severity,
        )

    def _check_invariants(
        self, output: EngineOutput, change: StateChange, now: datetime
    ) -> None:
        """Условия, которые обязаны держаться до конца сценария."""
        for invariant in self._sop.invariants:
            if invariant.variable != change.variable:
                continue
            if invariant.after_step not in self._completed:
                continue
            if change.value == invariant.must_equal:
                continue

            step = self._step_by_id(invariant.after_step)
            self._errors += 1
            if step.id not in self._failed:
                self._failed.append(step.id)
            self._status[step.id] = StepStatus.FAILED
            self._verdict_of[step.id] = VerdictKind.MISMATCH
            # Условие шага снова нарушено — он больше не выполнен.
            self._completed.discard(step.id)
            self._rejected.pop(step.id, None)

            verdict = self._verdict(
                step,
                VerdictKind.MISMATCH,
                invariant.severity,
                ReasonCode.UNEXPECTED_STATE,
                confidence=change.confidence,
                message=invariant.message.strip(),
                details={
                    "invariant": invariant.id,
                    "variable": invariant.variable,
                    "expected": invariant.must_equal,
                    "actual": change.value,
                },
                now=now,
                change=change,
            )
            output.verdicts.append(verdict)
            self._blocked_message = invariant.message.strip()
            self._say(
                output,
                invariant.message.strip(),
                UtteranceKind.CRITICAL
                if invariant.severity is Severity.CRITICAL
                else UtteranceKind.WARNING,
                priority=Priority.INTERRUPT,
                verdict_id=verdict.event_id,
                source_frame_ts=change.first_seen_ts,
            )
            log.warning("сеанс %s: нарушен инвариант '%s'", self.session_id, invariant.id)

    def _confidence_settled(self, step: SopStep, confidence: float, now: datetime) -> bool:
        """Можно ли уже засчитывать шаг с такой уверенностью.

        Порог шага работает как отсрочка, а не как отказ. В момент подтверждения
        состояния окно сглаживания ещё наполовину состоит из кадров самого
        движения, поэтому уверенность занижена и через секунду дорастает.
        Выдать здесь «не разобрал» значило бы соврать инструктору о правильно
        выполненном действии, а отказаться засчитывать — застопорить тренировку
        из-за собственной неуверенности.
        """
        if step.min_confidence is None or confidence >= step.min_confidence:
            self._low_confidence_since.pop(step.id, None)
            return True

        since = self._low_confidence_since.setdefault(step.id, now)
        waited = (now - since).total_seconds()
        if waited < self._config.low_confidence_grace_sec:
            return False

        # Пауза выдержана, состояние держится — засчитываем. Пометка о низкой
        # уверенности уходит в детали вердикта и в отчёт.
        log.info(
            "шаг '%s' засчитан с уверенностью %.2f ниже порога %.2f (ждали %.1f с)",
            step.id,
            confidence,
            step.min_confidence,
            waited,
        )
        self._low_confidence_since.pop(step.id, None)
        return True

    def _resolve_pending(self, output: EngineOutput, now: datetime) -> None:
        """Дозреть шагам, отложенным по уверенности.

        Нужно потому, что состояние может больше не меняться: труба доехала до
        дна, новых наблюдений о смене положения не будет, и без этой проверки
        отложенный шаг ждал бы сторожевого таймера.
        """
        for step_id in list(self._low_confidence_since):
            if step_id in self._completed:
                self._low_confidence_since.pop(step_id, None)
                continue
            step = self._step_by_id(step_id)
            if not self._expectations_met(step, voice_intent=None):
                self._low_confidence_since.pop(step_id, None)
                continue
            if self._unmet_requirements(step):
                continue
            confidence = self._step_confidence(step, None)
            if self._confidence_settled(step, confidence, now):
                self._accept(output, step, None, now, confidence=confidence)
                self._catch_up(output, now)

    def _emit_unclear(self, output: EngineOutput, change: StateChange, now: datetime) -> None:
        if self.state is not SessionState.RUNNING or not self._unclear_allowed(now):
            return
        step = self.current_step()
        verdict = self._verdict(
            step,
            VerdictKind.UNCLEAR,
            Severity.INFO,
            ReasonCode.LOW_CONFIDENCE,
            confidence=0.0,
            message=f"Не удалось разобрать состояние: {change.variable}",
            details={"variable": change.variable, "agreement": change.agreement},
            now=now,
            change=change,
        )
        output.verdicts.append(verdict)
        if self.mode is not Mode.TESTING or self._config.announce_unclear_in_testing:
            self._say(output, self._sop.phrases.unclear, UtteranceKind.WARNING)

    def _end_blindness(self, now: datetime) -> None:
        """Вернуть обучаемому время, которое система его не видела.

        Потеря видеопотока — отказ оборудования, а не бездействие человека.
        Голос обещает «проверка приостановлена», и срок шага обязан вести
        себя так же: он сдвигается на всю длительность молчания источника.
        """
        if self._blind_since is None:
            return
        shift = now - self._blind_since
        self._blind_since = None
        if self._deadline is not None:
            self._deadline += shift
        step = self.current_step()
        if step is not None and step.id in self._started_step_at:
            self._started_step_at[step.id] += shift

    def _emit_stream_lost(
        self, output: EngineOutput, change: StateChange, now: datetime
    ) -> None:
        if self.state is not SessionState.RUNNING or not self._unclear_allowed(now):
            return
        step = self.current_step()
        verdict = self._verdict(
            step,
            VerdictKind.UNCLEAR,
            Severity.WARNING,
            ReasonCode.STREAM_LOST,
            confidence=0.0,
            message="Потерян видеопоток, оценка действий приостановлена",
            details={"variable": change.variable},
            now=now,
        )
        output.verdicts.append(verdict)
        self._say(
            output,
            self._sop.phrases.stream_lost,
            UtteranceKind.WARNING,
            priority=Priority.INTERRUPT,
            cache_key="scenario.stream_lost",
        )

    def _unclear_allowed(self, now: datetime) -> bool:
        """Ограничение частоты вердиктов UNCLEAR.

        Неопределённость по своей природе повторяется, и без ограничения она
        вытеснила бы из журнала и из очереди речи всё содержательное.
        """
        if self._last_unclear_at is not None:
            if (now - self._last_unclear_at).total_seconds() < self._config.unclear_cooldown_sec:
                return False
        self._last_unclear_at = now
        return True

    # ------------------------------------------------------------------
    # Шаги и завершение
    # ------------------------------------------------------------------

    def _enter_step(self, output: EngineOutput, now: datetime) -> None:
        step = self.current_step()
        if step is None:
            return
        self._status.setdefault(step.id, StepStatus.PENDING)
        if self._status[step.id] is StepStatus.PENDING:
            self._status[step.id] = StepStatus.ACTIVE
        self._started_step_at.setdefault(step.id, now)
        # Молчание источника до начала шага к этому шагу не относится: иначе
        # простой стенда перед занятием достался бы обучаемому как запас
        # времени, и сторожевой таймер не сработал бы вовсе.
        self._blind_since = None
        self._deadline = (
            now + timedelta(seconds=step.timeout_sec) if step.timeout_sec else None
        )

        request = self._say(
            output,
            step.phrases.instruction,
            UtteranceKind.INSTRUCTION,
            cache_key=f"step.{step.id}.instruction",
        )
        # Инструкцию запоминаем всегда, даже если в этом режиме её не озвучили:
        # команда «повтори» должна работать и в тестовом режиме.
        self._last_instruction = request or SpeakRequest(
            session_id=self.session_id,
            text=step.phrases.instruction,
            kind=UtteranceKind.INSTRUCTION,
            cache_key=f"step.{step.id}.instruction",
        )

    def _finish(self, output: EngineOutput, now: datetime) -> None:
        self.state = SessionState.FINISHED
        self._finished_ts = now
        self._deadline = None
        self._say(
            output,
            self._sop.phrases.finished,
            UtteranceKind.REPORT,
            cache_key="scenario.finished",
        )
        log.info(
            "сеанс %s: завершён, ошибок %d, длительность %.0f с",
            self.session_id,
            self._errors,
            (now - self._started_ts).total_seconds() if self._started_ts else 0,
        )

    def _reset_steps(self) -> None:
        self._completed = set()
        self._status = {step.id: StepStatus.PENDING for step in self._sop.steps}
        self._verdict_of = {}
        self._started_step_at = {}
        self._finished_step_at = {}
        self._rejected = {}
        self._low_confidence_since = {}
        self._last_instruction = None
        self._last_unclear_at = None

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    def current_step(self) -> SopStep | None:
        if 0 <= self._current < len(self._sop.steps):
            return self._sop.steps[self._current]
        return None

    def _step_by_id(self, step_id: str) -> SopStep:
        for step in self._sop.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def _consequence_phrase(self, missing: list[str]) -> str:
        consequence = self._step_by_id(missing[0]).consequence
        titles = ", ".join(f"«{self._step_by_id(sid).title}»" for sid in missing)
        base = f"Нарушен порядок. Сначала необходимо выполнить: {titles}."
        return f"{base} {consequence.strip()}" if consequence else base

    def _details(self, step: SopStep, change: StateChange | None) -> dict:
        details: dict = {"step_title": step.title, "critical": step.critical}
        if change is not None:
            details.update(
                {
                    "variable": change.variable,
                    "value": change.value,
                    "agreement": round(change.agreement, 3),
                    "observations": change.observations,
                }
            )
        return details

    def _verdict(
        self,
        step: SopStep | None,
        kind: VerdictKind,
        severity: Severity,
        reason: ReasonCode,
        *,
        confidence: float,
        message: str,
        details: dict,
        now: datetime,
        change: StateChange | None = None,
    ) -> Verdict:
        latency_ms: float | None = None
        if change is not None:
            # От момента, когда новое состояние впервые стало видно, до вердикта.
            # Это честная мера: она включает окно подтверждения, которое и есть
            # основная статья расхода бюджета п. 6.2 ТЗ.
            latency_ms = max(0.0, (now - change.first_seen_ts).total_seconds() * 1000)

        return Verdict(
            session_id=self.session_id,
            step_id=step.id if step else "",
            step_index=self._sop.step_position(step.id) + 1 if step else 0,
            kind=kind,
            severity=severity,
            reason_code=reason,
            confidence=max(0.0, min(1.0, confidence)),
            message=message,
            details=details,
            source_event_id=change.event_id if change else None,
            latency_ms=latency_ms,
        )

    def _say(
        self,
        output: EngineOutput,
        text: str,
        kind: UtteranceKind,
        *,
        priority: Priority = Priority.NORMAL,
        verdict_id: str | None = None,
        cache_key: str | None = None,
        source_frame_ts: datetime | None = None,
        force: bool = False,
    ) -> SpeakRequest | None:
        if not force and kind not in _SPEECH_POLICY[self.mode]:
            return None
        request = SpeakRequest(
            session_id=self.session_id,
            text=" ".join(text.split()),
            kind=kind,
            priority=priority,
            cache_key=cache_key,
            verdict_id=verdict_id,
            source_frame_ts=source_frame_ts.isoformat() if source_frame_ts else None,
        )
        output.speech.append(request)
        return request

    # ------------------------------------------------------------------

    def snapshot(self) -> SessionSnapshot:
        steps: list[StepView] = []
        for index, step in enumerate(self._sop.steps):
            steps.append(
                StepView(
                    id=step.id,
                    index=index + 1,
                    title=step.title,
                    critical=step.critical,
                    status=self._status.get(step.id, StepStatus.PENDING),
                    last_verdict=self._verdict_of.get(step.id),
                    started_ts=self._started_step_at.get(step.id),
                    finished_ts=self._finished_step_at.get(step.id),
                    deadline_ts=self._deadline if index == self._current else None,
                )
            )

        current = self.current_step()
        return SessionSnapshot(
            session_id=self.session_id,
            sop_id=self._sop.id,
            sop_title=self._sop.title,
            scenario_code=self._sop.scenario_code,
            mode=self.mode,
            state=self.state,
            trainee_name=self.trainee_name,
            current_step_id=current.id if current else None,
            current_step_index=self._current + 1 if current else 0,
            steps=steps,
            errors_count=self._errors,
            failed_step_ids=list(self._failed),
            blocked_message=self._blocked_message,
            observed_state=dict(self._observed),
            stream_online=self._stream_online,
            started_ts=self._started_ts,
            finished_ts=self._finished_ts,
        )

    @property
    def errors_count(self) -> int:
        return self._errors

    @property
    def failed_steps(self) -> list[str]:
        return list(self._failed)

    @property
    def blocked_message(self) -> str | None:
        return self._blocked_message
