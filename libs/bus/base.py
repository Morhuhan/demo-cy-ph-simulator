"""Абстракция шины сообщений.

Две реализации за одним интерфейсом (п. 1.1 вопросника Заказчика):

* `RedisStreamBus` — поставочная. Каждый сервис отдельный процесс, шина внешняя,
  consumer groups дают подтверждение обработки и переживают перезапуск сервиса.
* `InMemoryBus` — для локального прогона без Docker и для тестов. Все сервисы
  живут в одном процессе, шина — asyncio-очереди.

Композиция выбирается в точке входа, сами сервисы про реализацию не знают.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TypeVar

from pydantic import ValidationError

from libs.contracts import Envelope

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Envelope)


class Bus(ABC):
    """Интерфейс шины. Публикация по имени потока, подписка через consumer group."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def publish(self, stream: str, message: Envelope) -> None: ...

    @abstractmethod
    def subscribe(
        self,
        stream: str,
        model: type[T],
        *,
        group: str,
        consumer: str = "main",
    ) -> AsyncIterator[T]:
        """Асинхронный итератор по сообщениям потока.

        Сообщения, не проходящие валидацию по `model`, логируются и
        пропускаются — один кривой пакет от смежников не должен ронять сервис.
        """

    async def __aenter__(self) -> Bus:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def decode(raw: str | bytes, model: type[T], *, stream: str) -> T | None:
    """Разбор payload с проверкой контракта. None — если сообщение невалидно."""
    try:
        message = model.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("поток %s: сообщение не прошло валидацию контракта: %s", stream, exc)
        return None

    expected_major = Envelope.model_fields["schema_version"].default.split(".")[0]
    actual_major = message.schema_version.split(".")[0]
    if actual_major != expected_major:
        log.warning(
            "поток %s: несовместимая версия схемы %s (ожидалась %s.x), сообщение отброшено",
            stream,
            message.schema_version,
            expected_major,
        )
        return None

    return message
