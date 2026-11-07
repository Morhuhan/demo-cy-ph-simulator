"""Сервис сглаживания: подписан на сырой поток CV, публикует смены состояния.

Вынесен в отдельный сервис сознательно. Это та граница, где алгоритм захочется
поменять первым — и менять его надо будет, не трогая ни CV, ни сценарный движок.
"""

from __future__ import annotations

import asyncio
import logging

from libs.bus import CV_HEALTH, CV_OBSERVATIONS, STATE_CHANGES, Bus
from libs.config import AppConfig
from libs.contracts import Observation, StreamHealth, StreamState

from .smoothing import Smoother

log = logging.getLogger(__name__)

SERVICE_NAME = "event_processor"
CONSUMER_GROUP = "event-processor"

STALE_CHECK_PERIOD_SEC = 1.0


class EventProcessorService:
    def __init__(self, bus: Bus, config: AppConfig) -> None:
        self._bus = bus
        self._config = config
        self.smoother = Smoother(config.smoothing)
        self._tasks: list[asyncio.Task[None]] = []
        self._stream_online = True
        self._paused = False

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume_observations(), name="ep-observations"),
            asyncio.create_task(self._consume_health(), name="ep-health"),
            asyncio.create_task(self._watch_stale(), name="ep-stale"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def reset(self) -> None:
        self.smoother.reset()

    def pause(self) -> None:
        """Занятие приостановлено: наблюдений не будет, и это не отказ камеры."""
        self._paused = True

    def resume(self) -> None:
        # Окно сглаживания за паузу устарело целиком: решать по кадрам,
        # снятым до остановки, нельзя. Начинаем набирать заново — так же,
        # как после восстановления оборванного потока.
        self._paused = False
        self.smoother.reset()

    async def _consume_observations(self) -> None:
        async for observation in self._bus.subscribe(
            CV_OBSERVATIONS, Observation, group=CONSUMER_GROUP
        ):
            try:
                if not self._stream_online:
                    # Поток восстановился: наблюдения снова идут.
                    self._stream_online = True
                change = self.smoother.observe(observation)
                if change is not None:
                    await self._bus.publish(STATE_CHANGES, change)
            except Exception:  # pragma: no cover
                log.exception("сбой при обработке наблюдения")

    async def _consume_health(self) -> None:
        async for health in self._bus.subscribe(CV_HEALTH, StreamHealth, group=CONSUMER_GROUP):
            self._stream_online = health.state is StreamState.ONLINE
            if not self._stream_online:
                # Буфер устарел: держать его смысла нет, а после восстановления
                # он дал бы решение по кадрам до обрыва.
                self.smoother.reset()
                log.warning("поток %s: %s", health.state, health.reason or "без причины")

    async def _watch_stale(self) -> None:
        while True:
            await asyncio.sleep(STALE_CHECK_PERIOD_SEC)
            if self._paused:
                continue
            try:
                for change in self.smoother.check_stale():
                    await self._bus.publish(STATE_CHANGES, change)
            except Exception:  # pragma: no cover
                log.exception("сбой при проверке устаревших переменных")
