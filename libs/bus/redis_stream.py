"""Шина на Redis Streams — поставочная реализация для docker-compose.

Почему Redis Streams, а не NATS: consumer groups и персистентность из коробки,
одна зависимость на весь стек, и тот же Redis работает кэшем синтезированных
фраз TTS. NATS выигрывал бы при разнесении по нескольким площадкам — такой
задачи в проекте нет.

Потоки подрезаются по длине (MAXLEN ~), чтобы сырой поток наблюдений не съел
диск за смену. Долговременное хранение — задача storage_api, а не шины.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TypeVar

from libs.contracts import Envelope

from .base import Bus, decode

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Envelope)

DEFAULT_MAXLEN = 50_000
BLOCK_MS = 5_000


class RedisStreamBus(Bus):
    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        maxlen: int = DEFAULT_MAXLEN,
    ) -> None:
        self._url = url
        self._maxlen = maxlen
        self._redis = None  # type: ignore[var-annotated]
        self._ensured_groups: set[tuple[str, str]] = set()

    async def start(self) -> None:
        import redis.asyncio as redis  # импорт здесь, чтобы локальный режим не требовал redis

        self._redis = redis.from_url(self._url, decode_responses=True)
        await self._redis.ping()
        log.info("шина подключена: %s", self._url)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def publish(self, stream: str, message: Envelope) -> None:
        if self._redis is None:
            raise RuntimeError("шина не запущена: вызовите start()")
        await self._redis.xadd(
            stream,
            {"payload": message.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def _ensure_group(self, stream: str, group: str) -> None:
        assert self._redis is not None
        if (stream, group) in self._ensured_groups:
            return
        try:
            # mkstream=True — группа создаётся даже для ещё не существующего
            # потока. Группа читает с "$", то есть только новые сообщения:
            # после перезапуска сервиса не нужно переваривать всё, что накопилось
            # за смену. Обратная сторона — сообщения, опубликованные до создания
            # группы, потребитель не увидит, поэтому в docker-compose источник
            # наблюдений поднимается после потребителей.
            await self._redis.xgroup_create(stream, group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP — группа уже есть, это норма
            if "BUSYGROUP" not in str(exc):
                raise
        self._ensured_groups.add((stream, group))

    async def subscribe(  # type: ignore[override]
        self,
        stream: str,
        model: type[T],
        *,
        group: str,
        consumer: str = "main",
    ) -> AsyncIterator[T]:
        if self._redis is None:
            raise RuntimeError("шина не запущена: вызовите start()")
        await self._ensure_group(stream, group)

        while True:
            response = await self._redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=64,
                block=BLOCK_MS,
            )
            if not response:
                continue

            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    payload = fields.get("payload")
                    if payload is not None:
                        message = decode(payload, model, stream=stream)
                        if message is not None:
                            yield message
                    # Подтверждаем в любом случае: невалидное сообщение уже
                    # залогировано, переобрабатывать его бесконечно бессмысленно.
                    await self._redis.xack(stream, group, entry_id)
