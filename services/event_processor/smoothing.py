"""Сглаживание потока наблюдений.

Задача: из пятнадцати сообщений в секунду сделать несколько смен состояния за
весь сценарий. Без этого труба, зависшая между «над горловиной» и «опущена до
упора», породила бы десятки вердиктов на один физический шаг.

Механизм — скользящее окно с требованием согласия. Значение считается
подтверждённым, если внутри окна:

  * набралось достаточно наблюдений (защита от редкого потока);
  * доля согласных с ним наблюдений не ниже `min_agreement`;
  * средняя уверенность этих наблюдений не ниже `min_confidence`.

Именно требование согласия и гасит дребезг: пока модель мечется между двумя
близкими состояниями, каждое из них набирает около половины окна и ни одно не
проходит порог 0.85. Система молчит ровно до тех пор, пока картинка не
устоится. Отдельного «окна отпускания» не нужно — оно только увеличило бы
задержку, ничего не добавив.

`min_hold_sec` — нижний порог против пинг-понга у самой границы порога. На
нормальном ходу сценария он ни на что не влияет: шаги разделены секундами.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from libs.config import SmoothingConfig, SmoothingProfile
from libs.contracts import Observation, StateChange, StateQuality, utc_now

log = logging.getLogger(__name__)


@dataclass
class _Sample:
    frame_ts: datetime
    value: str | None
    confidence: float
    unclear: bool


@dataclass
class _VariableState:
    """Состояние сглаживателя по одной переменной."""

    buffer: deque[_Sample] = field(default_factory=lambda: deque(maxlen=600))
    confirmed: str | None = None
    confirmed_at: datetime | None = None
    ambiguous_since: datetime | None = None
    last_unclear_report: datetime | None = None
    last_observation_at: datetime | None = None
    reported_stale: bool = False
    source: str = ""
    camera_id: str = ""
    post_id: str | None = None


@dataclass
class SmoothingStats:
    """Счётчики для журнала и для замеров на приёмке."""

    observations: int = 0
    changes: int = 0
    unclear_reports: int = 0
    suppressed: int = 0  # наблюдения, не приведшие к смене состояния


class Smoother:
    """Превращает сырой поток наблюдений в подтверждённые смены состояния."""

    def __init__(self, config: SmoothingConfig) -> None:
        self._config = config
        self._variables: dict[str, _VariableState] = {}
        self.stats = SmoothingStats()

    def state_of(self, variable: str) -> str | None:
        entry = self._variables.get(variable)
        return entry.confirmed if entry else None

    def snapshot(self) -> dict[str, str | None]:
        return {name: entry.confirmed for name, entry in self._variables.items()}

    def observe(self, observation: Observation) -> StateChange | None:
        """Принять наблюдение. Вернуть смену состояния, если она произошла."""
        self.stats.observations += 1

        entry = self._variables.setdefault(observation.variable, _VariableState())
        entry.source = observation.source
        entry.camera_id = observation.camera_id
        entry.post_id = observation.post_id
        entry.last_observation_at = observation.frame_ts
        entry.reported_stale = False
        entry.buffer.append(
            _Sample(
                frame_ts=observation.frame_ts,
                value=observation.value,
                confidence=observation.confidence,
                unclear=observation.unclear,
            )
        )

        profile = self._config.profile_for(observation.variable)
        self._prune(entry, profile, observation.frame_ts)

        change = self._evaluate(observation.variable, entry, profile, observation.frame_ts)
        if change is None:
            self.stats.suppressed += 1
        return change

    def check_stale(self, now: datetime | None = None) -> list[StateChange]:
        """Переменные, по которым давно нет наблюдений.

        Молчание источника — это не «состояние не изменилось», а отсутствие
        данных. Движок должен знать разницу.
        """
        now = now or utc_now()
        result: list[StateChange] = []

        for variable, entry in self._variables.items():
            profile = self._config.profile_for(variable)
            if entry.last_observation_at is None or entry.reported_stale:
                continue
            silence = (now - entry.last_observation_at).total_seconds()
            if silence < profile.stale_after_sec:
                continue

            entry.reported_stale = True
            result.append(
                StateChange(
                    variable=variable,
                    value=None,
                    previous_value=entry.confirmed,
                    quality=StateQuality.STALE,
                    confidence=0.0,
                    agreement=0.0,
                    first_seen_ts=entry.last_observation_at,
                    confirmed_ts=now,
                    observations=0,
                    source=entry.source,
                    camera_id=entry.camera_id,
                    post_id=entry.post_id,
                )
            )
            log.info("переменная %s: наблюдений нет %.1f с", variable, silence)

        return result

    def reset(self) -> None:
        self._variables.clear()
        self.stats = SmoothingStats()

    # ------------------------------------------------------------------

    def _prune(self, entry: _VariableState, profile: SmoothingProfile, now: datetime) -> None:
        horizon = now - timedelta(seconds=profile.confirm_window_sec)
        while entry.buffer and entry.buffer[0].frame_ts < horizon:
            entry.buffer.popleft()

    def _evaluate(
        self,
        variable: str,
        entry: _VariableState,
        profile: SmoothingProfile,
        now: datetime,
    ) -> StateChange | None:
        window = list(entry.buffer)
        if len(window) < profile.min_observations:
            return None

        valued = [sample for sample in window if not sample.unclear and sample.value is not None]
        if not valued:
            return self._maybe_report_unclear(variable, entry, profile, now, agreement=0.0)

        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for sample in valued:
            assert sample.value is not None
            totals[sample.value] = totals.get(sample.value, 0.0) + sample.confidence
            counts[sample.value] = counts.get(sample.value, 0) + 1

        best = max(totals, key=lambda value: totals[value])
        # Знаменатель — всё окно, включая «не разобрал»: неразобранные кадры
        # тоже свидетельствуют против уверенного вывода.
        agreement = counts[best] / len(window)
        mean_confidence = totals[best] / counts[best]

        if agreement < profile.min_agreement or mean_confidence < profile.min_confidence:
            return self._maybe_report_unclear(variable, entry, profile, now, agreement=agreement)

        # Пороги взяты — картинка ясная. Счётчик неопределённости сбрасывается
        # здесь, а не только при смене значения: иначе одно неудачное окно
        # взводило бы таймер навсегда и через unclear_after_sec давало ложное
        # «не разобрал» на совершенно спокойной сцене.
        entry.ambiguous_since = None

        if best == entry.confirmed:
            return None

        if entry.confirmed_at is not None:
            held = (now - entry.confirmed_at).total_seconds()
            if held < profile.min_hold_sec:
                return None

        first_seen = min(sample.frame_ts for sample in valued if sample.value == best)
        previous = entry.confirmed
        entry.confirmed = best
        entry.confirmed_at = now
        entry.ambiguous_since = None
        entry.last_unclear_report = None
        self.stats.changes += 1

        log.info(
            "переменная %s: %s -> %s (согласие %.0f%%, уверенность %.2f, задержка окна %.0f мс)",
            variable,
            previous,
            best,
            agreement * 100,
            mean_confidence,
            (now - first_seen).total_seconds() * 1000,
        )

        return StateChange(
            variable=variable,
            value=best,
            previous_value=previous,
            quality=StateQuality.CONFIRMED,
            confidence=mean_confidence,
            agreement=agreement,
            first_seen_ts=first_seen,
            confirmed_ts=now,
            observations=len(window),
            source=entry.source,
            camera_id=entry.camera_id,
            post_id=entry.post_id,
        )

    def _maybe_report_unclear(
        self,
        variable: str,
        entry: _VariableState,
        profile: SmoothingProfile,
        now: datetime,
        *,
        agreement: float,
    ) -> StateChange | None:
        """Сообщить, что переменная не разбирается — но только если это всерьёз.

        Два фильтра. Первый: неопределённость должна продержаться
        `unclear_after_sec`. Мгновенный провал согласия — это либо обычный
        физический переход между состояниями, либо одно неудачное окно на
        трёхпроцентном шуме распознавания; ни то ни другое инструктору
        показывать не надо. Второй фильтр — частота повторов, иначе восемь
        секунд дребезга дали бы сотню одинаковых строк в журнале.
        """
        if entry.ambiguous_since is None:
            entry.ambiguous_since = now
            return None

        if (now - entry.ambiguous_since).total_seconds() < profile.unclear_after_sec:
            return None

        if entry.last_unclear_report is not None:
            elapsed = (now - entry.last_unclear_report).total_seconds()
            if elapsed < profile.unclear_report_sec:
                return None

        entry.last_unclear_report = now
        self.stats.unclear_reports += 1

        return StateChange(
            variable=variable,
            value=None,
            previous_value=entry.confirmed,
            quality=StateQuality.UNCLEAR,
            confidence=0.0,
            agreement=agreement,
            first_seen_ts=entry.buffer[0].frame_ts if entry.buffer else now,
            confirmed_ts=now,
            observations=len(entry.buffer),
            source=entry.source,
            camera_id=entry.camera_id,
            post_id=entry.post_id,
        )
