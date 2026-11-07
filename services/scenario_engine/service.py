"""Привязка сценарного движка к шине."""

from __future__ import annotations

import asyncio
import logging

from libs.bus import (
    SESSION_COMMANDS,
    SESSION_SNAPSHOTS,
    SPEAK_REQUESTS,
    STATE_CHANGES,
    VERDICTS,
    VOICE_COMMANDS,
    Bus,
)
from libs.config import AppConfig
from libs.contracts import (
    CommandName,
    SessionCommand,
    SopSpec,
    StateChange,
    VoiceCommand,
)

from .engine import EngineOutput, ScenarioEngine

log = logging.getLogger(__name__)

SERVICE_NAME = "scenario_engine"
CONSUMER_GROUP = "scenario-engine"

#: Период опроса сторожевых таймеров. Секунда достаточна: тайм-ауты шагов
#: измеряются десятками секунд, а лишняя точность стоила бы холостых циклов.
TICK_PERIOD_SEC = 1.0


class ScenarioEngineService:
    def __init__(self, bus: Bus, sop: SopSpec, config: AppConfig) -> None:
        self._bus = bus
        self._config = config
        self.engine = ScenarioEngine(sop, config.engine)
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        #: Вызывается при каждой смене состояния сеанса. По нему точка сборки
        #: ведёт источник наблюдений: он работает, только пока идёт занятие.
        #:
        #: Важно, что не только на команды инструктора: сценарий завершается и
        #: сам — пройденным до конца или остановленным критической блокировкой,
        #: — и об этом узнать неоткуда, кроме как отсюда.
        self.on_session_change = None
        self._last_state = None

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume_states(), name="engine-states"),
            asyncio.create_task(self._consume_voice(), name="engine-voice"),
            asyncio.create_task(self._consume_commands(), name="engine-commands"),
            asyncio.create_task(self._watchdog(), name="engine-watchdog"),
        ]
        await self._publish(EngineOutput(snapshot=self.engine.snapshot()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def switch_scenario(self, sop: SopSpec) -> None:
        """Сменить сценарий на ходу.

        Движок пересобирается целиком: накопленное состояние прошлого сценария
        к новому отношения не имеет, а переносить его частями — верный способ
        получить вердикт по шагу, которого в новом сценарии нет.
        """
        async with self._lock:
            self.engine = ScenarioEngine(sop, self._config.engine)
            output = EngineOutput(snapshot=self.engine.snapshot())
        await self._publish(output)
        log.info("сценарий переключён на %s", sop.id)

    async def _publish(self, output: EngineOutput) -> None:
        for verdict in output.verdicts:
            await self._bus.publish(VERDICTS, verdict)
        for request in output.speech:
            await self._bus.publish(SPEAK_REQUESTS, request)
        if output.snapshot is not None:
            await self._bus.publish(SESSION_SNAPSHOTS, output.snapshot)
        self._notify_state()

    def _notify_state(self) -> None:
        if self.engine.state is self._last_state:
            return
        self._last_state = self.engine.state
        if self.on_session_change is not None:
            self.on_session_change(self.engine.session_id, self.engine.state)

    async def _consume_states(self) -> None:
        async for change in self._bus.subscribe(STATE_CHANGES, StateChange, group=CONSUMER_GROUP):
            try:
                async with self._lock:
                    output = self.engine.on_state_change(change)
                await self._publish(output)
            except Exception:  # pragma: no cover
                log.exception("сбой при обработке состояния %s", change.variable)

    async def _consume_voice(self) -> None:
        async for command in self._bus.subscribe(
            VOICE_COMMANDS, VoiceCommand, group=CONSUMER_GROUP
        ):
            try:
                async with self._lock:
                    output = self.engine.on_voice(command)
                await self._publish(output)
            except Exception:  # pragma: no cover
                log.exception("сбой при обработке голосовой команды")

    async def _consume_commands(self) -> None:
        async for command in self._bus.subscribe(
            SESSION_COMMANDS, SessionCommand, group=CONSUMER_GROUP
        ):
            try:
                async with self._lock:
                    output = self._apply_command(command)
                await self._publish(output)
            except Exception:  # pragma: no cover
                log.exception("сбой при выполнении команды %s", command.command)

    def _apply_command(self, command: SessionCommand) -> EngineOutput:
        match command.command:
            case CommandName.START:
                return self.engine.start(command.mode, command.trainee_name)
            case CommandName.STOP:
                return self.engine.stop()
            case CommandName.RESET:
                self._last_state = None
                return self.engine.reset()
            case CommandName.PAUSE:
                return self.engine.pause()
            case CommandName.RESUME:
                return self.engine.resume()
        return EngineOutput()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(TICK_PERIOD_SEC)
            try:
                async with self._lock:
                    output = self.engine.tick()
                if output.verdicts or output.speech:
                    await self._publish(output)
            except Exception:  # pragma: no cover
                log.exception("сбой сторожевого таймера")
