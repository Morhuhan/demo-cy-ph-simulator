"""Имена потоков шины и модели, которые по ним ходят.

Один поток — один тип сообщения. Смешивать типы в одном потоке нельзя:
потребители разбирают payload строго по объявленной модели.
"""

from __future__ import annotations

from typing import Final

from libs.contracts import (
    Observation,
    SessionCommand,
    SessionSnapshot,
    SpeakRequest,
    SpokenEvent,
    StateChange,
    StreamHealth,
    Verdict,
    VoiceCommand,
)

# Сырой поток от CV-модуля (в демо — от эмулятора). ~15 сообщений/с на переменную.
CV_OBSERVATIONS: Final = "cv.observations"
# Явные сообщения CV о собственных отказах: обрыв, засветка, обмерзание.
CV_HEALTH: Final = "cv.health"

# Выход сглаживания: только подтверждённые смены состояния.
STATE_CHANGES: Final = "state.changes"

# Решения сценарного движка.
VERDICTS: Final = "engine.verdicts"
# Полный снимок сеанса на каждое изменение.
SESSION_SNAPSHOTS: Final = "engine.session"
# Команды инструктора: старт, стоп, сброс.
SESSION_COMMANDS: Final = "engine.commands"

# Голосовой канал наружу.
SPEAK_REQUESTS: Final = "voice.speak"
SPOKEN_EVENTS: Final = "voice.spoken"
# Голосовой канал внутрь: «повтори», «помощь», доклад о готовности.
VOICE_COMMANDS: Final = "voice.commands"

STREAM_MODELS: Final[dict[str, type]] = {
    CV_OBSERVATIONS: Observation,
    CV_HEALTH: StreamHealth,
    STATE_CHANGES: StateChange,
    VERDICTS: Verdict,
    SESSION_SNAPSHOTS: SessionSnapshot,
    SESSION_COMMANDS: SessionCommand,
    SPEAK_REQUESTS: SpeakRequest,
    SPOKEN_EVENTS: SpokenEvent,
    VOICE_COMMANDS: VoiceCommand,
}

# Потоки, где важна свежесть, а не полнота: при переполнении отбрасываем старое,
# как это делает захват видео со сбросом устаревших кадров.
DROP_OLDEST_STREAMS: Final[frozenset[str]] = frozenset({CV_OBSERVATIONS})
