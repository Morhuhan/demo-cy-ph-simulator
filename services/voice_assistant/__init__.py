"""Голосовой ИИ-ассистент: синтез, очередь реплик, распознавание команд."""

from .service import VoiceAssistantService, create_router
from .stt import SttEngine, match_intent
from .tts import TtsEngine, create_tts

__all__ = [
    "SttEngine",
    "TtsEngine",
    "VoiceAssistantService",
    "create_router",
    "create_tts",
    "match_intent",
]
