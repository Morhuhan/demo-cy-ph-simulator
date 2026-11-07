"""Контракты обмена и распознавание голосовых команд."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.bus import InMemoryBus
from libs.contracts import Observation, VoiceIntent, utc_now
from services.voice_assistant.stt import match_intent


def test_unclear_observation_must_have_no_value():
    with pytest.raises(ValidationError, match="value должно быть null"):
        Observation(
            source="cv",
            camera_id="cam",
            variable="pipe_position",
            value="seated",
            confidence=0.0,
            unclear=True,
        )


def test_clear_observation_must_have_value():
    with pytest.raises(ValidationError, match="value обязательно"):
        Observation(
            source="cv", camera_id="cam", variable="pipe_position", confidence=0.9, unclear=False
        )


def test_confidence_bounded():
    with pytest.raises(ValidationError):
        Observation(
            source="cv",
            camera_id="cam",
            variable="pipe_position",
            value="seated",
            confidence=1.5,
        )


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("операторная, к наливу готов", VoiceIntent.READY_REPORT),
        ("готов", VoiceIntent.READY_REPORT),
        ("доклад о готовности", VoiceIntent.READY_REPORT),
        ("повтори", VoiceIntent.REPEAT),
        ("ещё раз", VoiceIntent.REPEAT),
        ("помощь", VoiceIntent.HELP),
        ("подскажи что дальше", VoiceIntent.HELP),
        ("", VoiceIntent.UNKNOWN),
        ("проверка связи раз два три", VoiceIntent.UNKNOWN),
    ],
)
def test_intent_matching(phrase, expected):
    intent, _ = match_intent(phrase)
    assert intent is expected


def test_ready_report_wins_over_repeat():
    """«Готов, повтори маршрут» — это доклад, а не просьба повторить."""
    intent, _ = match_intent("готов, повтори маршрут")
    assert intent is VoiceIntent.READY_REPORT


async def test_bus_roundtrip():
    bus = InMemoryBus()
    await bus.start()

    stream = bus.subscribe("test.stream", Observation, group="g")
    await bus.publish(
        "test.stream",
        Observation(
            source="cv",
            camera_id="cam",
            frame_ts=utc_now(),
            variable="grounding_lamp",
            value="green",
            confidence=0.9,
        ),
    )

    received = await anext(stream)
    assert received.value == "green"
    await bus.close()


async def test_bus_drops_invalid_messages():
    """Кривой пакет от смежников не должен ронять потребителя."""
    from libs.bus.base import decode

    assert decode('{"мусор": true}', Observation, stream="test") is None
