"""Шина на asyncio-очередях — для локального запуска без Docker и для тестов.

Семантика повторяет Redis Streams в той части, которая важна сервисам:
подписчики объединяются в consumer groups, каждая группа получает свою копию
сообщения, внутри группы сообщения распределяются между потребителями.

Чего здесь намеренно нет: истории потока и переживания перезапуска. Локальный
режим — один процесс, перезапускать в нём нечего.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import TypeVar

from libs.contracts import Envelope

from .base import Bus, decode
from .streams import DROP_OLDEST_STREAMS

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Envelope)

DEFAULT_QUEUE_SIZE = 2000


class InMemoryBus(Bus):
    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        # stream -> group -> список очередей потребителей группы
        self._groups: dict[str, dict[str, list[asyncio.Queue[str]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._round_robin: dict[tuple[str, str], int] = defaultdict(int)
        self._closed = False
        self._dropped: dict[str, int] = defaultdict(int)

    async def start(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        for groups in self._groups.values():
            for queues in groups.values():
                for queue in queues:
                    # Разбудить всех ожидающих, чтобы итераторы завершились.
                    queue.put_nowait(_SENTINEL)
        for stream, count in self._dropped.items():
            if count:
                log.info("поток %s: отброшено %d сообщений при переполнении", stream, count)

    async def publish(self, stream: str, message: Envelope) -> None:
        if self._closed:
            raise RuntimeError("шина закрыта")

        payload = message.model_dump_json()
        drop_oldest = stream in DROP_OLDEST_STREAMS

        for group, queues in self._groups[stream].items():
            if not queues:
                continue
            # Внутри группы — по кругу, как consumer group в Redis.
            key = (stream, group)
            index = self._round_robin[key] % len(queues)
            self._round_robin[key] = index + 1
            queue = queues[index]

            if queue.full():
                if drop_oldest:
                    # Свежесть важнее полноты: выбрасываем самое старое наблюдение.
                    try:
                        queue.get_nowait()
                        self._dropped[stream] += 1
                    except asyncio.QueueEmpty:  # pragma: no cover - гонка маловероятна
                        pass
                else:
                    # Для решений и команд терять сообщения нельзя — ждём места.
                    await queue.put(payload)
                    continue
            queue.put_nowait(payload)

    def subscribe(
        self,
        stream: str,
        model: type[T],
        *,
        group: str,
        consumer: str = "main",
    ) -> AsyncIterator[T]:
        """Подписаться на поток.

        Метод намеренно не корутина: очередь регистрируется прямо здесь, в
        момент вызова. Если бы регистрация жила в теле асинхронного генератора,
        она откладывалась бы до первой итерации, и всё опубликованное в
        промежутке терялось бы — гонка, которая проявляется ровно на старте
        стенда и ровно один раз из десяти.
        """
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._groups[stream][group].append(queue)
        log.debug("подписка %s/%s на поток %s", group, consumer, stream)

        async def iterate() -> AsyncIterator[T]:
            try:
                while True:
                    payload = await queue.get()
                    if payload == _SENTINEL:
                        return
                    message = decode(payload, model, stream=stream)
                    if message is not None:
                        yield message
            finally:
                queues = self._groups[stream][group]
                if queue in queues:
                    queues.remove(queue)

        return iterate()


_SENTINEL = "__close__"
