"""Эмулятор потока наблюдений — временная замена модуля компьютерного зрения.

Устроен так, что скриптованный прогон и ручной пульт — это один и тот же
механизм с разными источниками команд. Эмулятор ведёт «истинное» состояние
оборудования и на каждом такте показывает его так, как показывала бы неидеальная
модель: с разбросом уверенности, случайными путаницами соседних состояний,
кадрами «не разобрал» и дребезгом на переходах.

Важно, что грязь здесь настраиваемая и воспроизводимая. Если бы эмулятор отдавал
чистые состояния, отлаженная на нём система развалилась бы на первом реальном
видео — а сглаживание, ради которого всё и затевалось, осталось бы непроверенным.
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from libs.config import EmulatorConfig
from libs.contracts import (
    Observation,
    SopSpec,
    StreamHealth,
    StreamState,
    VoiceIntent,
    utc_now,
)

from .detector import Detector, DetectorMessage
from .runs import EmulatorRun, NoiseProfile

log = logging.getLogger(__name__)


@dataclass
class _Transition:
    """Физический переход между состояниями — он не мгновенный."""

    old: str
    new: str
    start: datetime
    end: datetime


@dataclass
class _Flicker:
    between: tuple[str, str]
    end: datetime
    confidence_mean: float


@dataclass
class _RunProgress:
    run: EmulatorRun
    started: datetime
    next_event: int = 0
    finished: bool = False
    #: Момент сцены, когда прогон закончился. Нужен, чтобы счётчик времени
    #: замер на месте, а не тикал до бесконечности после остановки.
    finished_at: datetime | None = None


@dataclass
class EmulatorState:
    """Снимок эмулятора для пульта инструктора."""

    mode: str
    true_state: dict[str, str]
    stream: StreamState
    run_id: str | None = None
    run_elapsed_sec: float = 0.0
    run_title: str | None = None
    note: str | None = None
    noise: NoiseProfile = field(default_factory=NoiseProfile)
    paused: bool = False


class EmulatorDetector(Detector):
    """Источник наблюдений, управляемый прогоном или вручную."""

    source_id = "cv-emulator"

    def __init__(
        self,
        sop: SopSpec,
        config: EmulatorConfig,
        *,
        seed: int | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        # clock внедряется, чтобы приёмочные прогоны можно было гонять в
        # ускоренном модельном времени: минутный сценарий проверяется за
        # доли секунды и даёт тот же результат, что в реальном времени.
        self._sop = sop
        self._config = config
        self._clock = clock
        self._random = random.Random(seed)

        self._true_state: dict[str, str] = sop.initial_state()
        self._transitions: dict[str, _Transition] = {}
        self._flickers: dict[str, _Flicker] = {}
        self._noise = NoiseProfile()
        self._stream = StreamState.ONLINE
        self._stream_reason: str | None = None
        self._stream_changed = True  # отправить StreamHealth на старте

        # Сценическое время: оно замирает на паузе, реальное — нет. По
        # сценическому идут прогон, переходы состояний и дребезг; по реальному
        # проставляется время кадра. Камера на паузе продолжает снимать,
        # просто в кадре ничего не меняется.
        self._paused_at: datetime | None = None
        self._paused_total = timedelta()

        self._progress: _RunProgress | None = None
        self._pending_voice: list[VoiceIntent] = []
        self._note: str | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Управление: пульт инструктора и скриптованные прогоны
    # ------------------------------------------------------------------

    def scene_now(self, now: datetime | None = None) -> datetime:
        """Время сцены: реальное за вычетом накопленной паузы."""
        if self._paused_at is not None:
            return self._paused_at - self._paused_total
        return (now or self._clock()) - self._paused_total

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._clock()
            log.info("эмулятор: пауза")

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None
            log.info("эмулятор: продолжение")

    def snapshot(self) -> EmulatorState:
        elapsed = 0.0
        if self._progress is not None:
            end = self._progress.finished_at or self.scene_now()
            elapsed = (end - self._progress.started).total_seconds()
        return EmulatorState(
            mode="run" if self._progress and not self._progress.finished else "manual",
            true_state=dict(self._true_state),
            stream=self._stream,
            run_id=self._progress.run.id if self._progress else None,
            run_title=self._progress.run.title if self._progress else None,
            run_elapsed_sec=round(elapsed, 1),
            note=self._note,
            noise=self._noise,
            paused=self._paused_at is not None,
        )

    def set_state(self, variable: str, value: str, *, transition_sec: float = 0.6) -> None:
        """Ручное переключение состояния — нажатие тумблера на пульте."""
        observable = self._sop.observables.get(variable)
        if observable is None:
            raise KeyError(f"неизвестная переменная '{variable}'")
        if value not in observable.values:
            raise ValueError(
                f"значение '{value}' недопустимо для '{variable}'; "
                f"ожидалось одно из {sorted(observable.values)}"
            )
        self._begin_transition(variable, value, transition_sec)

    def set_stream(self, state: StreamState, reason: str | None = None) -> None:
        if self._stream != state:
            self._stream = state
            self._stream_reason = reason
            self._stream_changed = True

    def emit_voice(self, intent: VoiceIntent) -> None:
        """Эмуляция реплики оператора.

        В реальной системе эту команду породит STT. Здесь — пульт или прогон,
        потому что седьмой шаг СОП камерой не наблюдается в принципе.
        """
        self._pending_voice.append(intent)

    def start_run(self, run: EmulatorRun) -> None:
        self.reset()
        self._noise = run.noise.model_copy()
        self._progress = _RunProgress(run=run, started=self.scene_now())
        log.info("эмулятор: старт прогона '%s' (%s)", run.id, run.title)

    def stop_run(self) -> None:
        if self._progress is not None and not self._progress.finished:
            self._progress.finished = True
            self._progress.finished_at = self.scene_now()

    def reset(self) -> None:
        self._true_state = self._sop.initial_state()
        self._transitions.clear()
        self._flickers.clear()
        self._pending_voice.clear()
        self._progress = None
        self._note = None
        self._noise = NoiseProfile()
        self._paused_at = None
        self.set_stream(StreamState.ONLINE)
        self._stream_changed = True

    # ------------------------------------------------------------------
    # Поток наблюдений
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def stream(self) -> AsyncIterator[DetectorMessage]:  # type: ignore[override]
        import asyncio

        self._running = True
        period = 1.0 / self._config.rate_hz

        while self._running:
            for message in self.tick():
                yield message
            await asyncio.sleep(period)

    def tick(self) -> list[DetectorMessage]:
        """Один такт: продвинуть прогон и выдать наблюдения по всем переменным."""
        now = self._clock()
        scene = self.scene_now(now)
        messages: list[DetectorMessage] = []

        self._advance_run(scene)

        if self._stream_changed:
            self._stream_changed = False
            messages.append(
                StreamHealth(
                    source=self.source_id,
                    camera_id=self._config.camera_id,
                    state=self._stream,
                    reason=self._stream_reason,
                )
            )

        if self._stream is StreamState.LOST:
            # Потока нет — наблюдений нет. Молчание источника движок отследит
            # сторожевым таймером, но явное сообщение выше уже отправлено.
            return messages

        if self._paused_at is not None:
            # Пауза замораживает и распознавание: кадры не поступают, лента
            # событий стоит. Молчание источника здесь штатное, и отличить его
            # от обрыва камеры обязана принимающая сторона — она знает, что
            # занятие приостановлено.
            return messages

        for variable in self._sop.observables:
            messages.append(self._observe(variable, now, scene))

        return messages

    def take_pending_voice(self) -> list[VoiceIntent]:
        """Забрать накопленные реплики оператора (их публикует сервис)."""
        pending, self._pending_voice = self._pending_voice, []
        return pending

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _begin_transition(self, variable: str, value: str, duration_sec: float) -> None:
        current = self._true_state.get(variable)
        if current == value:
            return
        now = self.scene_now()
        if duration_sec > 0 and current is not None:
            self._transitions[variable] = _Transition(
                old=current,
                new=value,
                start=now,
                end=now + timedelta(seconds=duration_sec),
            )
        self._true_state[variable] = value

    def _advance_run(self, now: datetime) -> None:
        progress = self._progress
        if progress is None or progress.finished:
            return

        elapsed = (now - progress.started).total_seconds()

        while progress.next_event < len(progress.run.timeline):
            event = progress.run.timeline[progress.next_event]
            if event.at > elapsed:
                break
            progress.next_event += 1

            if event.note:
                self._note = event.note
                log.info("эмулятор [%0.1f с]: %s", event.at, event.note)
            if event.noise is not None:
                self._noise = event.noise.model_copy()
            if event.set:
                for variable, value in event.set.items():
                    self._begin_transition(variable, value, event.transition_sec)
            if event.flicker is not None:
                spec = event.flicker
                self._flickers[spec.variable] = _Flicker(
                    between=(spec.between[0], spec.between[1]),
                    end=now + timedelta(seconds=spec.duration_sec),
                    confidence_mean=spec.confidence_mean,
                )
            if event.stream is not None:
                self.set_stream(event.stream, event.stream_reason)
            if event.voice is not None:
                self._pending_voice.append(event.voice)

        if elapsed >= progress.run.duration_sec:
            progress.finished = True
            progress.finished_at = progress.started + timedelta(
                seconds=progress.run.duration_sec
            )
            log.info("эмулятор: прогон '%s' завершён", progress.run.id)

    def _observe(self, variable: str, now: datetime, scene: datetime) -> Observation:
        """Как неидеальная модель видит переменную в этот момент.

        `now` — время кадра, идёт всегда. `scene` — время сцены, на паузе стоит:
        по нему считаются переходы и дребезг, чтобы движение объекта замирало
        вместе со сценарием.
        """
        values = list(self._sop.observables[variable].values)
        truth = self._true_state[variable]
        confidence_mean = self._noise.confidence_mean
        value = truth

        flicker = self._flickers.get(variable)
        if flicker is not None:
            if scene >= flicker.end:
                del self._flickers[variable]
            else:
                # Устойчивый дребезг: модель мечется между двумя близкими
                # состояниями. Это главный тест для сглаживания.
                value = self._random.choice(flicker.between)
                confidence_mean = flicker.confidence_mean

        transition = self._transitions.get(variable)
        if transition is not None:
            if scene >= transition.end:
                del self._transitions[variable]
            else:
                span = (transition.end - transition.start).total_seconds()
                progress = (scene - transition.start).total_seconds() / span if span else 1.0
                value = transition.new if self._random.random() < progress else transition.old
                # Во время движения модель уверена заметно хуже.
                confidence_mean *= 0.7

        if self._random.random() < self._noise.unclear_prob:
            return Observation(
                source=self.source_id,
                camera_id=self._config.camera_id,
                post_id=self._config.post_id,
                frame_ts=now,
                variable=variable,
                value=None,
                confidence=0.0,
                unclear=True,
            )

        if self._random.random() < self._noise.flicker_prob:
            value = self._neighbour(values, value)
            confidence_mean *= 0.75

        confidence = self._random.gauss(confidence_mean, self._noise.confidence_jitter)
        return Observation(
            source=self.source_id,
            camera_id=self._config.camera_id,
            post_id=self._config.post_id,
            frame_ts=now,
            variable=variable,
            value=value,
            confidence=min(1.0, max(0.0, confidence)),
            unclear=False,
        )

    def _neighbour(self, values: list[str], value: str) -> str:
        """Соседнее значение — модели путают близкие состояния, а не случайные.

        Труба путается между `over_hatch` и `seated`, а не между
        `stowed` и `bottom`.
        """
        if len(values) < 2:
            return value
        index = values.index(value)
        candidates = [i for i in (index - 1, index + 1) if 0 <= i < len(values)]
        return values[self._random.choice(candidates)]
