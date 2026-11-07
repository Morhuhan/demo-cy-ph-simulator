"""Сервис-источник наблюдений: гонит поток эмулятора в шину и даёт пульт управления.

В поставочной сборке этот сервис заменяется реальным CV-модулем. Всё, что он
обязан делать, — публиковать Observation в поток `cv.observations` и StreamHealth
в `cv.health` по контракту из contracts/.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from libs.bus import CV_HEALTH, CV_OBSERVATIONS, VOICE_COMMANDS, Bus
from libs.config import AppConfig
from libs.contracts import (
    Observation,
    SopSpec,
    StreamHealth,
    StreamState,
    VoiceCommand,
    VoiceIntent,
)

from .emulator import EmulatorDetector
from .runs import EmulatorRun, load_runs

log = logging.getLogger(__name__)

SERVICE_NAME = "cv_adapter"

#: Как маршрут находит службу своего зрителя — см. libs/stand.py.
Resolve = Callable[[Request], Awaitable["CvAdapterService"]]


class CvAdapterService:
    def __init__(self, bus: Bus, sop: SopSpec, config: AppConfig) -> None:
        self._bus = bus
        self._sop = sop
        self._config = config
        self.detector = EmulatorDetector(sop, config.emulator)
        self.runs: dict[str, EmulatorRun] = {}
        self._task: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        #: Идёт ли съёмка. Источник работает, только пока идёт занятие: вне его
        #: распознавать нечего, а лента событий, бегущая при остановленном
        #: занятии, выглядит как работающая система на пустом месте.
        #: По умолчанию True — в раздельной сборке сеансом никто не управляет.
        self.streaming = True
        #: Источник наблюдений — свойство сюжета. У сценария с реальной съёмкой
        #: эмулятор молчит: два источника по одной переменной несовместимы.
        self.enabled_for_scenario = True

    def load_runs(self) -> None:
        from libs.config import project_root

        directory = project_root() / self._config.emulator.runs_dir
        if directory.exists():
            self.runs = load_runs(directory, scenario=self._sop.id)
            log.info(
                "эмулятор: загружено прогонов — %d (сценарий %s)", len(self.runs), self._sop.id
            )
        else:
            log.warning("каталог прогонов не найден: %s", directory)

    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.load_runs()
        if not self.active:
            # На интеграции источником наблюдений служит реальный CV-модуль.
            # Оставить эмулятор включённым нельзя: два источника публикуют по
            # одной переменной, ни одно значение не набирает согласия в окне
            # сглаживания, и система замирает на «не разобрал».
            log.info("эмулятор выключен — источником наблюдений служит реальный CV-модуль")
            return
        await self.detector.start()
        self._task = asyncio.create_task(self._publish_loop(), name="cv-adapter-publish")

    @property
    def active(self) -> bool:
        """Работает ли эмулятор при текущей конфигурации и сюжете."""
        return self._config.emulator.enabled and self.enabled_for_scenario

    async def stop(self) -> None:
        await self.detector.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def switch_scenario(self, sop: SopSpec) -> None:
        """Сменить сценарий: другой набор наблюдаемых переменных и прогонов.

        Нужен ли эмулятор — решает сюжет, а не то, работал ли он до сих пор.
        У сюжета с записями он молчит, у сюжета без записей обязан заговорить,
        даже если на прошлом сюжете его не было.
        """
        await self.stop()
        self._sop = sop
        self.detector = EmulatorDetector(sop, self._config.emulator)
        # start() сам перечитает прогоны и поднимет поток, если источник нужен.
        await self.start()

    async def say(self, intent: VoiceIntent) -> None:
        """Реплика обучаемого — прямо в шину, мимо эмулятора.

        Речь не наблюдается камерой, поэтому к источнику наблюдений отношения
        не имеет: в реальной системе эту команду порождает распознавание речи.
        Публикуем напрямую, иначе при работе от записи реплика оседала бы в
        очереди эмулятора, которую некому разобрать, — и шаг с докладом стал
        бы недостижим.
        """
        await self._bus.publish(
            VOICE_COMMANDS,
            VoiceCommand(
                session_id=self._session_id,
                intent=intent,
                transcript=_TRANSCRIPTS.get(intent, ""),
                confidence=0.95,
                engine="instructor",
            ),
        )

    def bind_session(self, session_id: str | None) -> None:
        """Текущий сеанс — нужен только для маркировки голосовых команд."""
        self._session_id = session_id

    async def _publish_loop(self) -> None:
        period = 1.0 / self._config.emulator.rate_hz
        while True:
            try:
                if not self.streaming:
                    await asyncio.sleep(period)
                    continue
                for message in self.detector.tick():
                    if isinstance(message, Observation):
                        await self._bus.publish(CV_OBSERVATIONS, message)
                    elif isinstance(message, StreamHealth):
                        await self._bus.publish(CV_HEALTH, message)

                for intent in self.detector.take_pending_voice():
                    await self._bus.publish(
                        VOICE_COMMANDS,
                        VoiceCommand(
                            session_id=self._session_id,
                            intent=intent,
                            transcript=_TRANSCRIPTS.get(intent, ""),
                            confidence=0.95,
                            engine="emulator",
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - источник не должен падать молча
                log.exception("сбой в такте эмулятора")

            await asyncio.sleep(period)


# Как звучали бы эти команды в реальности — попадает в журнал вместо пустого текста.
_TRANSCRIPTS = {
    VoiceIntent.READY_REPORT: "операторная, к наливу готов",
    VoiceIntent.REPEAT: "повтори",
    VoiceIntent.HELP: "помощь",
}


# ----------------------------------------------------------------------
# Пульт: HTTP-управление эмулятором
# ----------------------------------------------------------------------


class SetStateRequest(BaseModel):
    variable: str
    value: str
    transition_sec: float = Field(default=0.6, ge=0)


class SetStreamRequest(BaseModel):
    state: StreamState
    reason: str | None = None


class VoiceRequest(BaseModel):
    intent: VoiceIntent


def create_router(resolve: Resolve) -> APIRouter:
    """Маршруты пульта.

    Занятий на стенде несколько, поэтому служба не одна: `resolve` находит ту,
    что принадлежит зрителю этого запроса.
    """
    router = APIRouter(prefix="/emulator", tags=["emulator"])

    @router.get("/state")
    async def get_state(request: Request) -> dict:
        service = await resolve(request)
        snapshot = service.detector.snapshot()
        return {
            # Ручное управление имеет смысл только у эмулятора. Когда источником
            # служит реальная съёмка, действия происходят в кадре, а тумблеры
            # пульта ни на что не влияют.
            "enabled": service.active,
            "mode": snapshot.mode,
            "true_state": snapshot.true_state,
            "stream": snapshot.stream,
            "run_id": snapshot.run_id,
            "run_title": snapshot.run_title,
            "run_elapsed_sec": snapshot.run_elapsed_sec,
            "paused": snapshot.paused,
            "note": snapshot.note,
            "observables": {
                name: {
                    "title": observable.title,
                    "values": {
                        value: meta.title for value, meta in observable.values.items()
                    },
                }
                for name, observable in service._sop.observables.items()
            },
        }

    @router.get("/runs")
    async def list_runs(request: Request) -> list[dict]:
        service = await resolve(request)
        # Когда источником служит реальный CV-модуль, эмулятор молчит, и его
        # прогоны запускать бессмысленно: кнопка была бы, а нажатие ничего
        # не делало бы.
        if not service.active:
            return []
        return [
            {
                "id": run.id,
                "title": run.title,
                "description": run.description,
                "duration_sec": run.duration_sec,
                "expected_count": len(run.expected),
            }
            for run in service.runs.values()
        ]

    @router.post("/runs/{run_id}/start")
    async def start_run(run_id: str, request: Request) -> dict:
        service = await resolve(request)
        run = service.runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"прогон '{run_id}' не найден")
        service.detector.start_run(run)
        return {"ok": True, "run_id": run_id}

    @router.post("/runs/stop")
    async def stop_run(request: Request) -> dict:
        service = await resolve(request)
        service.detector.stop_run()
        return {"ok": True}

    @router.post("/state")
    async def set_state(body: SetStateRequest, request: Request) -> dict:
        service = await resolve(request)
        try:
            service.detector.set_state(
                body.variable, body.value, transition_sec=body.transition_sec
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.post("/stream")
    async def set_stream(body: SetStreamRequest, request: Request) -> dict:
        service = await resolve(request)
        service.detector.set_stream(body.state, body.reason)
        return {"ok": True}

    @router.post("/voice")
    async def emit_voice(body: VoiceRequest, request: Request) -> dict:
        service = await resolve(request)
        await service.say(body.intent)
        return {"ok": True}

    @router.post("/reset")
    async def reset(request: Request) -> dict:
        service = await resolve(request)
        service.detector.reset()
        return {"ok": True}

    return router
