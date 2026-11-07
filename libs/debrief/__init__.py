"""Разбор занятия связным текстом.

Составитель выбирается конфигурацией. По умолчанию работает шаблон — он не
зависит ни от сети, ни от ключей. Языковая модель подключается одной настройкой
и подменяет только формулировку: вердикты к этому моменту уже вынесены движком
и в разборе не пересматриваются.
"""

from __future__ import annotations

import logging

from libs.config import DebriefConfig

from .base import Debrief, DebriefEvent, DebriefInput, Responder, minutes
from .llm import LlmError, LlmResponder
from .template import TemplateResponder

log = logging.getLogger(__name__)


def build_responder(config: DebriefConfig) -> Responder:
    if config.resolved_engine() == "llm":
        return LlmResponder(config)
    return TemplateResponder()


async def compose_debrief(config: DebriefConfig, data: DebriefInput) -> Debrief:
    """Составить разбор, откатившись на шаблон, если модель не ответила.

    Откат существует ради показа: отвалившаяся сеть не должна оставлять
    инструктора без разбора вовсе. Причина отката возвращается рядом с текстом,
    чтобы молчаливой подмены не было.
    """
    responder = build_responder(config)

    if isinstance(responder, TemplateResponder):
        return Debrief(text=await responder.compose(data), engine="template")

    try:
        return Debrief(text=await responder.compose(data), engine="llm")
    except LlmError as exc:
        log.warning("разбор моделью не удался (%s) — составляю по шаблону", exc)
        fallback = TemplateResponder()
        return Debrief(
            text=await fallback.compose(data),
            engine="template",
            fallback_reason=str(exc),
        )


__all__ = [
    "Debrief",
    "DebriefEvent",
    "DebriefInput",
    "LlmError",
    "LlmResponder",
    "Responder",
    "TemplateResponder",
    "build_responder",
    "compose_debrief",
    "minutes",
]
