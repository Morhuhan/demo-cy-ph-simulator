"""Шина сообщений: интерфейс и две реализации."""

from __future__ import annotations

import os

from .base import Bus
from .memory import InMemoryBus
from .redis_stream import RedisStreamBus
from .streams import (
    CV_HEALTH,
    CV_OBSERVATIONS,
    SESSION_COMMANDS,
    SESSION_SNAPSHOTS,
    SPEAK_REQUESTS,
    SPOKEN_EVENTS,
    STATE_CHANGES,
    STREAM_MODELS,
    VERDICTS,
    VOICE_COMMANDS,
)


def bus_from_env() -> Bus:
    """Выбор реализации по окружению.

    Задан REDIS_URL — берём Redis Streams (так работает docker-compose).
    Не задан — шина в памяти (так работает `python run_local.py`).
    """
    url = os.getenv("REDIS_URL", "").strip()
    return RedisStreamBus(url) if url else InMemoryBus()


__all__ = [
    "CV_HEALTH",
    "CV_OBSERVATIONS",
    "SESSION_COMMANDS",
    "SESSION_SNAPSHOTS",
    "SPEAK_REQUESTS",
    "SPOKEN_EVENTS",
    "STATE_CHANGES",
    "STREAM_MODELS",
    "VERDICTS",
    "VOICE_COMMANDS",
    "Bus",
    "InMemoryBus",
    "RedisStreamBus",
    "bus_from_env",
]
