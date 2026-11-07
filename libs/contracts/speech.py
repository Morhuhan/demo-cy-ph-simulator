"""Контракты голосового канала: что ассистент говорит и что слышит.

Голос двунаправленный. Наружу — инструкции и оценки. Внутрь — короткие команды
оператора и доклад на седьмом шаге СОП («сообщить в операторную о готовности»).
Седьмой шаг принципиально не наблюдаем камерой, он закрывается только речью.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from .common import Envelope


class UtteranceKind(StrEnum):
    INSTRUCTION = "instruction"  # что делать сейчас
    CONFIRMATION = "confirmation"  # шаг принят
    WARNING = "warning"  # ошибка, но сценарий продолжается
    CRITICAL = "critical"  # нарушение блокировки
    HINT = "hint"  # ответ на «помощь»
    REPORT = "report"  # итог сеанса


class Priority(StrEnum):
    NORMAL = "normal"  # встаёт в очередь
    INTERRUPT = "interrupt"  # прерывает текущую реплику и чистит очередь


class SpeakRequest(Envelope):
    """Просьба произнести реплику.

    Движок формулирует текст, но не занимается синтезом и не знает про очередь.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    text: str
    kind: UtteranceKind
    priority: Priority = Priority.NORMAL

    cache_key: str | None = Field(
        default=None,
        description=(
            "Ключ постоянной фразы для кэша TTS. Фразы с ключом синтезируются при "
            "старте (прогрев) и дальше отдаются мгновенно."
        ),
    )
    verdict_id: str | None = Field(default=None, description="Вердикт, породивший реплику")
    source_frame_ts: str | None = Field(
        default=None,
        description="ISO-время исходного кадра — чтобы замерить сквозную задержку п. 6.2 ТЗ",
    )


class SpokenEvent(Envelope):
    """Факт начала воспроизведения. Нужен для замера задержки и для журнала."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    request_id: str = Field(description="event_id соответствующего SpeakRequest")
    text: str
    kind: UtteranceKind
    engine: str = Field(description="Какая реализация TTS отработала", examples=["silero", "browser"])
    cache_hit: bool = False
    synth_ms: float = Field(ge=0.0, description="Время синтеза; 0 при попадании в кэш")
    duration_sec: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Сколько звучит реплика. По ней слушатели понимают, когда ассистент "
            "договорил: показ не перебивает голос на полуслове."
        ),
    )
    end_to_end_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="От frame_ts исходного кадра до начала реплики. Норматив п. 6.2 — 2000 мс.",
    )
    interrupted: str | None = Field(
        default=None, description="request_id реплики, которую эта прервала"
    )
    verdict_id: str | None = Field(
        default=None, description="Вердикт, породивший реплику"
    )


class VoiceIntent(StrEnum):
    REPEAT = "repeat"  # «повтори»
    HELP = "help"  # «помощь»
    READY_REPORT = "ready_report"  # доклад о готовности к наливу, шаг 7 СОП
    UNKNOWN = "unknown"


class VoiceCommand(Envelope):
    """Распознанная реплика оператора."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    intent: VoiceIntent
    transcript: str = Field(description="Распознанный текст как есть")
    confidence: float = Field(ge=0.0, le=1.0)
    engine: str = Field(description="Реализация STT", examples=["faster-whisper", "manual-ui"])
