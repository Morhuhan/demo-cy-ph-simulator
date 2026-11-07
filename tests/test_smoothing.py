"""Сглаживание потока наблюдений — то, что недооценивают и без чего система дёргается."""

from __future__ import annotations

from datetime import timedelta

from libs.config import SmoothingConfig, SmoothingProfile
from libs.contracts import Observation, StateQuality
from services.event_processor.smoothing import Smoother

from .conftest import EPOCH


def make_smoother(**overrides) -> Smoother:
    profile = SmoothingProfile(
        confirm_window_sec=1.0,
        min_confidence=0.6,
        min_agreement=0.8,
        min_hold_sec=0.3,
        stale_after_sec=3.0,
        unclear_after_sec=2.5,
        unclear_report_sec=3.0,
        min_observations=5,
        **overrides,
    )
    return Smoother(SmoothingConfig(defaults=profile))


def feed(smoother: Smoother, values, *, start=EPOCH, hz=15.0, confidence=0.9, variable="x"):
    """Скормить последовательность значений с равным шагом по времени."""
    changes = []
    period = 1.0 / hz
    for index, value in enumerate(values):
        observation = Observation(
            source="test",
            camera_id="cam",
            frame_ts=start + timedelta(seconds=index * period),
            variable=variable,
            value=value,
            confidence=confidence,
            unclear=value is None,
        )
        change = smoother.observe(observation)
        if change is not None:
            changes.append(change)
    return changes


def test_steady_state_confirmed_once():
    smoother = make_smoother()
    changes = feed(smoother, ["a"] * 40)
    assert len(changes) == 1
    assert changes[0].value == "a"
    assert changes[0].quality is StateQuality.CONFIRMED


def test_single_outlier_ignored():
    """Один ошибочный кадр на тридцать не должен ничего менять."""
    values = ["a"] * 15 + ["b"] + ["a"] * 15
    changes = feed(make_smoother(), values)
    assert [change.value for change in changes] == ["a"]


def test_jitter_produces_no_state_change():
    """Труба мечется между двумя состояниями — система обязана молчать.

    Это главное свойство сглаживания: пока ни одно значение не набирает
    нужной доли согласия в окне, подтверждённое состояние не меняется.
    """
    smoother = make_smoother()
    settle = feed(smoother, ["a"] * 20)
    assert [change.value for change in settle] == ["a"]

    jitter = ["a", "b"] * 60  # восемь секунд дребезга
    changes = feed(smoother, jitter, start=EPOCH + timedelta(seconds=20 / 15))
    assert all(change.quality is not StateQuality.CONFIRMED for change in changes), (
        "на дребезге не должно быть подтверждённых смен состояния"
    )


def test_state_settles_after_jitter():
    """После дребезга устоявшееся состояние всё же подтверждается."""
    smoother = make_smoother()
    feed(smoother, ["a"] * 20)
    offset = timedelta(seconds=20 / 15)
    feed(smoother, ["a", "b"] * 40, start=EPOCH + offset)
    offset += timedelta(seconds=80 / 15)
    changes = feed(smoother, ["b"] * 30, start=EPOCH + offset)
    assert [change.value for change in changes if change.quality is StateQuality.CONFIRMED] == [
        "b"
    ]


def test_low_confidence_not_confirmed():
    changes = feed(make_smoother(), ["a"] * 30, confidence=0.3)
    assert not [c for c in changes if c.quality is StateQuality.CONFIRMED]


def test_brief_transition_reports_no_unclear():
    """Обычный переход между состояниями — не повод говорить «не разобрал»."""
    smoother = make_smoother()
    feed(smoother, ["a"] * 20)
    offset = timedelta(seconds=20 / 15)
    # Полторы секунды смешанных наблюдений — типичный физический переход.
    changes = feed(smoother, ["a", "b"] * 11, start=EPOCH + offset)
    offset += timedelta(seconds=22 / 15)
    changes += feed(smoother, ["b"] * 25, start=EPOCH + offset)

    unclear = [c for c in changes if c.quality is StateQuality.UNCLEAR]
    assert not unclear, "короткий переход не должен порождать сообщений о неопределённости"


def test_sustained_ambiguity_reports_unclear():
    """А вот затяжная неопределённость обязана быть видна инструктору."""
    smoother = make_smoother()
    feed(smoother, ["a"] * 20)
    changes = feed(smoother, ["a", "b"] * 60, start=EPOCH + timedelta(seconds=20 / 15))
    assert [c for c in changes if c.quality is StateQuality.UNCLEAR]


def test_stale_when_source_goes_silent():
    """Молчание источника — это не «состояние не изменилось»."""
    smoother = make_smoother()
    feed(smoother, ["a"] * 20)
    last = EPOCH + timedelta(seconds=20 / 15)

    assert not smoother.check_stale(last + timedelta(seconds=1))
    stale = smoother.check_stale(last + timedelta(seconds=5))
    assert len(stale) == 1
    assert stale[0].quality is StateQuality.STALE
    assert stale[0].previous_value == "a"


def test_confirmation_latency_within_budget(config):
    """Задержка подтверждения не должна съедать бюджет п. 6.2 ТЗ."""
    smoother = Smoother(config.smoothing)
    changes = feed(smoother, ["over_hatch"] * 40, variable="pipe_position", confidence=0.9)
    assert changes
    lag_ms = (changes[0].confirmed_ts - changes[0].first_seen_ts).total_seconds() * 1000
    assert lag_ms <= config.latency_budget.smoothing_ms
