"""Сценарный движок: блокировки, инварианты, режимы, сторожевые таймеры."""

from __future__ import annotations

from datetime import timedelta

from libs.contracts import (
    Mode,
    Priority,
    ReasonCode,
    Severity,
    SessionState,
    StateChange,
    StateQuality,
    StepStatus,
    UtteranceKind,
    VerdictKind,
    VoiceCommand,
    VoiceIntent,
)
from services.scenario_engine.engine import ScenarioEngine

from .conftest import EPOCH


def change(variable: str, value: str, *, at, confidence=0.95, quality=StateQuality.CONFIRMED):
    return StateChange(
        variable=variable,
        value=value,
        quality=quality,
        confidence=confidence,
        agreement=1.0,
        first_seen_ts=at,
        confirmed_ts=at,
        observations=15,
        source="test",
        camera_id="cam",
    )


class Driver:
    """Прогон сценария по шагам в модельном времени."""

    def __init__(self, sop, config, mode=Mode.TRAINING):
        self.engine = ScenarioEngine(sop, config.engine)
        self.now = EPOCH
        self.verdicts = []
        self.speech = []
        self._collect(self.engine.start(mode=mode, trainee_name="Тест", now=self.now))

    def _collect(self, output):
        self.verdicts.extend(output.verdicts)
        self.speech.extend(output.speech)
        return output

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)
        self._collect(self.engine.tick(self.now))

    def observe(self, variable, value, *, confidence=0.95, after=2.0):
        self.advance(after)
        return self._collect(
            self.engine.on_state_change(
                change(variable, value, at=self.now, confidence=confidence), now=self.now
            )
        )

    def say(self, intent: VoiceIntent, *, after=2.0):
        self.advance(after)
        return self._collect(
            self.engine.on_voice(
                VoiceCommand(intent=intent, transcript="", confidence=0.9, engine="test"),
                now=self.now,
            )
        )

    def run_correctly(self):
        self.observe("grounding_lamp", "green")
        self.observe("hatch_cover", "open")
        self.observe("pipe_position", "over_hatch")
        self.observe("pipe_position", "seated")
        self.observe("clamp_weight", "pressed")
        self.observe("pipe_position", "bottom")
        self.say(VoiceIntent.READY_REPORT)

    @property
    def kinds(self):
        return [(v.step_id, v.kind) for v in self.verdicts]


# ----------------------------------------------------------------------


def test_correct_run_completes(sop, config):
    driver = Driver(sop, config)
    driver.run_correctly()

    assert driver.engine.state is SessionState.FINISHED
    assert driver.engine.errors_count == 0
    assert driver.kinds == [
        ("grounding", VerdictKind.MATCH),
        ("hatch", VerdictKind.MATCH),
        ("pipe_over", VerdictKind.MATCH),
        ("pipe_seated", VerdictKind.MATCH),
        ("clamp", VerdictKind.MATCH),
        ("pipe_bottom", VerdictKind.MATCH),
        ("report", VerdictKind.MATCH),
    ]


def test_hatch_without_grounding_is_critical(sop, config):
    """Главная блокировка регламента: искра при открытии люка без заземления."""
    driver = Driver(sop, config)
    driver.observe("hatch_cover", "open")

    verdict = driver.verdicts[-1]
    assert verdict.step_id == "hatch"
    assert verdict.kind is VerdictKind.MISMATCH
    assert verdict.severity is Severity.CRITICAL
    assert verdict.reason_code is ReasonCode.PRECONDITION_VIOLATED
    assert driver.engine.errors_count == 1


def test_critical_violation_speaks_immediately(sop, config):
    """Критическое предупреждение обрывает очередь, а не ждёт в ней."""
    driver = Driver(sop, config)
    driver.observe("hatch_cover", "open")

    critical = [r for r in driver.speech if r.kind is UtteranceKind.CRITICAL]
    assert critical
    assert critical[-1].priority is Priority.INTERRUPT


def test_violation_not_repeated(sop, config):
    """Одно нарушение — один вердикт, сколько бы состояний ни менялось потом."""
    driver = Driver(sop, config)
    driver.observe("hatch_cover", "open")
    driver.observe("pipe_position", "over_hatch")
    driver.observe("pipe_position", "seated")

    hatch_mismatches = [
        v for v in driver.verdicts if v.step_id == "hatch" and v.kind is VerdictKind.MISMATCH
    ]
    assert len(hatch_mismatches) == 1


def test_operator_can_recover_after_violation(sop, config):
    """Нарушение не запирает сценарий: исправился — продолжай."""
    driver = Driver(sop, config)
    driver.observe("hatch_cover", "open")
    driver.observe("hatch_cover", "closed")
    driver.observe("grounding_lamp", "green")
    driver.observe("hatch_cover", "open")

    assert ("grounding", VerdictKind.MATCH) in driver.kinds
    assert ("hatch", VerdictKind.MATCH) in driver.kinds
    assert driver.engine.blocked_message is None


def test_report_without_bottom_blocked(sop, config):
    """Доклад о готовности при неопущенной трубе — взрыв в цистерне по регламенту."""
    driver = Driver(sop, config)
    driver.observe("grounding_lamp", "green")
    driver.observe("hatch_cover", "open")
    driver.observe("pipe_position", "over_hatch")
    driver.observe("pipe_position", "seated")
    driver.observe("clamp_weight", "pressed")
    driver.say(VoiceIntent.READY_REPORT)

    verdict = driver.verdicts[-1]
    assert verdict.step_id == "report"
    assert verdict.kind is VerdictKind.MISMATCH
    assert verdict.severity is Severity.CRITICAL
    assert "pipe_bottom" in verdict.details["missing_steps"]


def test_grounding_invariant_fires(sop, config):
    """Заземление должно держаться весь сценарий, а не только на первом шаге."""
    driver = Driver(sop, config)
    driver.observe("grounding_lamp", "green")
    driver.observe("hatch_cover", "open")
    driver.observe("grounding_lamp", "off")

    verdict = driver.verdicts[-1]
    assert verdict.kind is VerdictKind.MISMATCH
    assert verdict.reason_code is ReasonCode.UNEXPECTED_STATE
    assert verdict.details["invariant"] == "grounding_holds"


def test_voice_report_alone_does_not_complete(sop, config):
    """Голосовое условие действует в момент реплики, а не накапливается."""
    driver = Driver(sop, config)
    driver.say(VoiceIntent.READY_REPORT)
    assert driver.engine.state is SessionState.RUNNING


def test_low_confidence_delays_but_accepts(sop, config):
    """Порог уверенности откладывает зачёт, а не отменяет его."""
    driver = Driver(sop, config)
    # Порог критического шага — 0.7, даём заведомо ниже.
    driver.observe("grounding_lamp", "green", confidence=0.62)
    assert not driver.verdicts, "сразу засчитывать не должны"

    driver.advance(config.engine.low_confidence_grace_sec + 0.5)
    verdict = driver.verdicts[-1]
    assert verdict.kind is VerdictKind.MATCH
    assert verdict.details["low_confidence"] is True


def test_timeout_fires_once_per_step(sop, config):
    driver = Driver(sop, config)
    grounding = sop.steps[0]
    driver.advance(grounding.timeout_sec + 2)
    driver.advance(grounding.timeout_sec + 2)

    timeouts = [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]
    assert len(timeouts) == 1
    assert timeouts[0].step_id == "grounding"


def test_stream_loss_is_not_operator_error(sop, config):
    """Пропавший поток — это UNCLEAR, а не ошибка обучаемого."""
    driver = Driver(sop, config)
    driver.observe("grounding_lamp", "green")
    driver.advance(2)
    driver._collect(
        driver.engine.on_state_change(
            change(
                "pipe_position", "stowed", at=driver.now, quality=StateQuality.STALE
            ).model_copy(update={"value": None}),
            now=driver.now,
        )
    )

    verdict = driver.verdicts[-1]
    assert verdict.kind is VerdictKind.UNCLEAR
    assert verdict.reason_code is ReasonCode.STREAM_LOST
    assert driver.engine.errors_count == 0


def test_blind_time_does_not_count_against_trainee(sop, config):
    """Пока система слепа, сторожевой таймер шага стоит.

    Голос при потере сигнала обещает, что проверка приостановлена. Если бы
    срок шага продолжал идти, обучаемому засчитали бы невыполнение за то
    время, когда его попросту не видели, — а отказ камеры не его вина.
    """
    driver = Driver(sop, config)
    grounding = sop.steps[0]

    driver.advance(1)
    driver._collect(
        driver.engine.on_state_change(
            change("grounding_lamp", None, at=driver.now, quality=StateQuality.STALE),
            now=driver.now,
        )
    )

    # Заведомо дольше отведённого шагу времени — но система ничего не видит.
    driver.advance(grounding.timeout_sec + 3)
    assert not [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]

    # Связь восстановилась: отсчёт продолжается с того же места, а не с нуля.
    driver.observe("grounding_lamp", "green", after=0.5)
    assert driver.verdicts[-1].kind is VerdictKind.MATCH
    assert not [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]


def test_silence_before_the_step_is_not_credited(sop, config):
    """Простой стенда до начала шага обучаемому не засчитывается.

    Пока занятия нет, источник молчит, и это молчание копилось бы как время
    слепоты. Тогда первое же наблюдение сдвинуло бы срок шага на весь простой,
    и сторожевой таймер не сработал бы никогда.
    """
    driver = Driver(sop, config)
    grounding = sop.steps[0]

    # Занятие останавливают — и стенд долго стоит без единого наблюдения.
    driver._collect(driver.engine.stop(now=driver.now))
    driver.advance(600)
    driver._collect(
        driver.engine.on_state_change(
            change("grounding_lamp", None, at=driver.now, quality=StateQuality.STALE),
            now=driver.now,
        )
    )
    driver.advance(600)

    driver._collect(driver.engine.start(trainee_name="Тест", now=driver.now))
    driver.advance(grounding.timeout_sec + 2)

    timeouts = [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]
    assert timeouts, "сторожевой таймер шага обязан сработать"


def test_testing_mode_stays_silent_on_success(sop, config):
    """Тестовый режим — самостоятельная работа: подтверждений не даём."""
    driver = Driver(sop, config, mode=Mode.TESTING)
    driver.run_correctly()

    kinds = {request.kind for request in driver.speech}
    assert UtteranceKind.INSTRUCTION not in kinds
    assert UtteranceKind.CONFIRMATION not in kinds


def test_testing_mode_still_warns(sop, config):
    """Но об опасном действии обязан сообщить и он."""
    driver = Driver(sop, config, mode=Mode.TESTING)
    driver.observe("hatch_cover", "open")
    assert any(request.kind is UtteranceKind.CRITICAL for request in driver.speech)


def test_help_works_in_testing_mode(sop, config):
    """Подсказку по прямому запросу выдаём в любом режиме."""
    driver = Driver(sop, config, mode=Mode.TESTING)
    driver.speech.clear()
    driver.say(VoiceIntent.HELP)
    assert any(request.kind is UtteranceKind.HINT for request in driver.speech)


def test_repeat_returns_current_instruction(sop, config):
    driver = Driver(sop, config, mode=Mode.TESTING)
    driver.speech.clear()
    driver.say(VoiceIntent.REPEAT)
    assert driver.speech
    assert driver.speech[-1].kind is UtteranceKind.INSTRUCTION


def test_snapshot_tracks_steps(sop, config):
    driver = Driver(sop, config)
    driver.observe("grounding_lamp", "green")
    snapshot = driver.engine.snapshot()

    by_id = {step.id: step for step in snapshot.steps}
    assert by_id["grounding"].status is StepStatus.DONE
    assert by_id["hatch"].status is StepStatus.ACTIVE
    assert snapshot.observed_state["grounding_lamp"] == "green"


def test_no_verdicts_before_session_starts(sop, config):
    """До старта состояние отслеживаем, но не оцениваем."""
    engine = ScenarioEngine(sop, config.engine)
    output = engine.on_state_change(change("hatch_cover", "open", at=EPOCH), now=EPOCH)
    assert not output.verdicts


# ----------------------------------------------------------------------
# Пауза занятия
# ----------------------------------------------------------------------


def test_pause_freezes_step_deadline(sop, config):
    """Пауза не должна съедать время, отведённое обучаемому на шаг.

    Сторожевой таймер первого шага — 120 с. Если поставить занятие на паузу
    на минуту, после продолжения у обучаемого обязано остаться столько же
    времени, сколько было в момент паузы.
    """
    driver = Driver(sop, config)
    grounding = sop.steps[0]

    driver.advance(10)
    driver.engine.pause(now=driver.now)
    assert driver.engine.state is SessionState.PAUSED

    # Минута простоя: тайм-аут сработать не должен.
    driver.now += timedelta(seconds=60)
    driver._collect(driver.engine.tick(driver.now))
    assert not [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]

    driver._collect(driver.engine.resume(now=driver.now))
    assert driver.engine.state is SessionState.RUNNING

    # Оставшегося времени шага ещё хватает…
    driver.advance(grounding.timeout_sec - 12)
    assert not [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]

    # …и только теперь срок выходит.
    driver.advance(4)
    timeouts = [v for v in driver.verdicts if v.kind is VerdictKind.TIMEOUT]
    assert len(timeouts) == 1
    assert timeouts[0].step_id == grounding.id


def test_paused_session_does_not_judge_actions(sop, config):
    """На паузе состояние отслеживается, но вердикты не выносятся."""
    driver = Driver(sop, config)
    driver.engine.pause(now=driver.now)

    driver.observe("grounding_lamp", "green")
    assert not driver.verdicts
    assert driver.engine.snapshot().observed_state["grounding_lamp"] == "green"

    # После продолжения тот же шаг закрывается штатно.
    driver._collect(driver.engine.resume(now=driver.now))
    driver.observe("grounding_lamp", "green")
    assert [v.kind for v in driver.verdicts] == [VerdictKind.MATCH]


def test_resume_repeats_current_instruction(sop, config):
    """Продолжая занятие, ассистент напоминает, на чём остановились."""
    driver = Driver(sop, config)
    driver.engine.pause(now=driver.now)
    before = len(driver.speech)

    driver._collect(driver.engine.resume(now=driver.now))
    said = driver.speech[before:]
    assert any(r.kind is UtteranceKind.INSTRUCTION for r in said)


def test_stop_from_pause_aborts_session(sop, config):
    """Остановка с паузы завершает занятие, а не оставляет его висеть."""
    driver = Driver(sop, config)
    driver.engine.pause(now=driver.now)
    driver._collect(driver.engine.stop(now=driver.now))
    assert driver.engine.state is SessionState.ABORTED


def test_paused_silence_is_not_stream_loss(sop, config):
    """На паузе источник молчит намеренно — это не отказ камеры.

    Пауза останавливает и распознавание, поэтому наблюдения перестают
    поступать и сглаживание объявляет переменные устаревшими. Принять это
    за потерю видеопотока нельзя: иначе каждая пауза на демонстрации
    заканчивалась бы сообщением о неисправности.
    """
    driver = Driver(sop, config)
    driver.observe("grounding_lamp", "green")
    driver.engine.pause(now=driver.now)

    driver.advance(1)
    driver._collect(
        driver.engine.on_state_change(
            change("grounding_lamp", None, at=driver.now, quality=StateQuality.STALE),
            now=driver.now,
        )
    )

    assert not [v for v in driver.verdicts if v.reason_code is ReasonCode.STREAM_LOST]
    assert driver.engine.snapshot().stream_online is True


# ----------------------------------------------------------------------
# Шаг-проверка состояния (СИЗ-01): уже надетая каска засчитывается на старте.
# ----------------------------------------------------------------------


def _ppe():
    from libs.config import load_config, load_sop

    config = load_config("configs/app-ppe.yaml")
    return load_sop(config.sop_path), config


def test_helmet_worn_before_start_passes_check():
    sop, config = _ppe()
    engine = ScenarioEngine(sop, config.engine)
    engine.on_state_change(change("helmet", "on", at=EPOCH), now=EPOCH)

    output = engine.start(mode=Mode.TRAINING, now=EPOCH + timedelta(seconds=1))

    assert [(v.step_id, v.kind) for v in output.verdicts] == [("helmet_check", VerdictKind.MATCH)]
    assert engine.snapshot().current_step_id == "ready_report"


def test_reset_keeps_observed_world_state():
    sop, config = _ppe()
    engine = ScenarioEngine(sop, config.engine)
    engine.start(mode=Mode.TRAINING, now=EPOCH)
    engine.on_state_change(change("helmet", "on", at=EPOCH), now=EPOCH)
    engine.stop(now=EPOCH + timedelta(seconds=2))

    engine.reset()
    output = engine.start(mode=Mode.TRAINING, now=EPOCH + timedelta(seconds=3))

    assert engine.snapshot().observed_state["helmet"] == "on"
    assert [(v.step_id, v.kind) for v in output.verdicts] == [("helmet_check", VerdictKind.MATCH)]


def test_helmet_off_at_start_waits_for_helmet():
    sop, config = _ppe()
    engine = ScenarioEngine(sop, config.engine)
    engine.on_state_change(change("helmet", "off", at=EPOCH), now=EPOCH)

    output = engine.start(mode=Mode.TRAINING, now=EPOCH + timedelta(seconds=1))

    assert output.verdicts == []
    assert engine.snapshot().current_step_id == "helmet_check"


def test_action_steps_are_not_credited_on_start(sop, config):
    # КФТ-01: заземление, подключённое до старта, не засчитывается само —
    # там важно действие обучаемого, а не состояние.
    engine = ScenarioEngine(sop, config.engine)
    engine.on_state_change(change("grounding_lamp", "green", at=EPOCH), now=EPOCH)

    output = engine.start(mode=Mode.TRAINING, now=EPOCH + timedelta(seconds=1))

    assert output.verdicts == []
