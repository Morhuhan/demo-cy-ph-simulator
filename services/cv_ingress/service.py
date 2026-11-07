"""Приём событий реального CV-модуля по WebSocket.

CV-модуль выступает клиентом и подключается сюда сам (его `transport.type:
websocket`, `url: ws://<хост>:<порт>/cv`), поэтому сервер поднимает сценарная
сторона.

Между контрактами есть содержательное расхождение: CV присылает только
подтверждённые изменения состояния, а сглаживание ниже по течению рассчитано
на непрерывный поток наблюдений и требует нескольких подтверждений в окне.
Одно событие на изменение такое окно никогда не наберёт.

Мостом служит удержание: ingress запоминает последнее состояние и повторяет
его с частотой `republish_hz`, пока CV не пришлёт следующее. Исходное событие
сохраняет время кадра от CV, повторы получают текущее время — благодаря этому
`first_seen_ts` смены состояния указывает на настоящий кадр, и замер задержки
по п. 6 ТЗ остаётся достоверным.

Обратная сторона удержания: замолчавший CV-модуль выглядел бы как спокойная
сцена. Но отличить одно от другого по одному лишь молчанию нельзя — CV шлёт
события только на смену состояния, и неподвижный кадр молчит законно сколько
угодно. Поэтому сторожевой таймер молчания включается только там, где смежник
обещал признак жизни (`heartbeat_expected`). Отвалившийся модуль ловится не им,
а разрывом соединения: он объявляет потерю сразу.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import ValidationError

from libs.bus import CV_HEALTH, CV_OBSERVATIONS, SPOKEN_EVENTS, VERDICTS, Bus
from libs.config import AppConfig
from libs.contracts import (
    BBox,
    Observation,
    SopSpec,
    SpokenEvent,
    StreamHealth,
    StreamState,
    Verdict,
    VerdictKind,
    utc_now,
)

from .replay import CvLog, CvLogPlayer, load_logs
from .translate import Translator
from .wire import ObjectStateEvent, ServiceEvent

log = logging.getLogger(__name__)

SERVICE_NAME = "cv_ingress"

#: Как маршрут находит службу своего зрителя — см. libs/stand.py.
Resolve = Callable[[Request | WebSocket], Awaitable["CvIngressService"]]

#: Пауза после реплики, прежде чем съёмка пойдёт дальше. Нужна, чтобы фраза
#: не сливалась с продолжением действия в кадре.
_SPEECH_TAIL_SEC = 0.6
#: Сколько ждать начала реплики после вердикта. Реплика встаёт в очередь и
#: может начаться не сразу. Если ассистент так и промолчал, съёмка продолжается
#: сама: замерший кадр на показе хуже пропущенной фразы.
_SPEECH_WAIT_SEC = 15.0


@dataclass(frozen=True, slots=True)
class _Held:
    """Последнее известное состояние переменной, которое ingress удерживает."""

    value: str | None
    confidence: float
    unclear: bool
    source: str
    camera_id: str
    bbox: BBox | None


class CvIngressService:
    def __init__(self, bus: Bus, sop: SopSpec, config: AppConfig) -> None:
        self._bus = bus
        self._config = config.cv_ingress
        #: Сценарий, под который подобраны записи. Источник наблюдений —
        #: свойство сюжета: у проверки СИЗ есть реальная съёмка, у налива её
        #: нет и работает эмулятор. Двух источников сразу быть не может.
        self._sop_id = sop.id
        self._translator = Translator(self._config, known_variables=set(sop.observables))

        self._held: dict[str, _Held] = {}
        self._last_event_at: datetime | None = None
        self._stream_lost = False
        self._clients = 0
        self._task: asyncio.Task[None] | None = None

        self.logs: dict[str, CvLog] = {}
        self.player = CvLogPlayer(self.handle_message, on_hold_end=self._hold_ended)
        #: До какого момента ассистент занят речью. Пока он говорит, съёмка
        #: стоит: перебить предупреждение на полуслове — потерять половину
        #: того, ради чего пауза и сделана.
        #: Когда договорена реплика по каждому вердикту.
        self._spoken_until: dict[str, float] = {}
        self._release_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._spoken_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    # ------------------------------------------------------------------

    async def start(self) -> None:
        from libs.config import project_root

        self.logs = load_logs(project_root() / self._config.logs_dir)
        if self.logs:
            log.info("записей CV загружено: %d", len(self.logs))

        if not self._config.enabled:
            log.info("приём событий CV выключен — стенд работает от эмулятора")
            return
        self._ensure_republish()
        self._ensure_watchers()
        log.info(
            "приём событий CV включён: ws %s, удержание %.0f Гц",
            self._config.path,
            self._config.republish_hz,
        )

    def _hold_ended(self, seconds: float) -> None:
        """Показ простоял эти секунды — на столько же отстали часы записи."""
        self._translator.shift_frame_epoch(seconds)

    def _ensure_republish(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._republish_loop(), name="cv-ingress-republish"
            )

    async def play_log(self, log_id: str) -> None:
        """Проиграть запись. Удержание поднимается, даже если живой приём выключен."""
        self.reset()
        self._ensure_republish()
        self._ensure_watchers()
        self.player.start(self.logs[log_id])

    def _ensure_watchers(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self._watch_verdicts(), name="cv-ingress-verdicts"
            )
        if self._spoken_task is None or self._spoken_task.done():
            self._spoken_task = asyncio.create_task(
                self._watch_spoken(), name="cv-ingress-spoken"
            )

    async def _watch_spoken(self) -> None:
        """Запоминать, когда договорена реплика по вердикту."""
        loop = asyncio.get_running_loop()
        async for event in self._bus.subscribe(
            SPOKEN_EVENTS, SpokenEvent, group="cv-ingress-spoken"
        ):
            if event.verdict_id is None:
                continue
            self._spoken_until[event.verdict_id] = (
                loop.time() + event.duration_sec + _SPEECH_TAIL_SEC
            )

    async def _release_when_spoken(self, verdict_id: str) -> None:
        """Продолжить съёмку, когда ассистент договорит именно об этом вердикте.

        Ждать всю очередь речи нельзя: за предупреждением может идти новая
        команда, и кадр простоял бы всё это время. Ждём ровно ту реплику,
        ради которой съёмка и остановлена.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SPEECH_WAIT_SEC
        while verdict_id not in self._spoken_until and loop.time() < deadline:
            await asyncio.sleep(0.1)

        ends_at = self._spoken_until.pop(verdict_id, 0.0)
        while loop.time() < ends_at:
            await asyncio.sleep(0.1)
        self.player.release()

    async def _watch_verdicts(self) -> None:
        """Снять паузу съёмки, когда система вынесла оценку.

        Запись останавливается на кадре «каска не надета» и ждёт, пока движок
        выдержит отведённое на шаг время и объявит нарушение. Так на показе
        видно причинную связь: сначала система реагирует, и только потом
        сотрудник исправляется.
        """
        async for verdict in self._bus.subscribe(
            VERDICTS, Verdict, group="cv-ingress-verdicts"
        ):
            if not self.player.holding:
                continue
            if verdict.kind in (VerdictKind.TIMEOUT, VerdictKind.MISMATCH):
                if self._release_task is None or self._release_task.done():
                    self._release_task = asyncio.create_task(
                        self._release_when_spoken(verdict.event_id),
                        name="cv-ingress-release",
                    )

    async def stop(self) -> None:
        self.player.stop()
        for task in (self._watch_task, self._spoken_task, self._release_task):
            if task is not None:
                task.cancel()
        self._watch_task = self._spoken_task = self._release_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def switch_scenario(self, sop: SopSpec) -> None:
        self.player.stop()
        self.reset()
        self._sop_id = sop.id
        self._translator = Translator(self._config, known_variables=set(sop.observables))

    def scenario_logs(self) -> dict[str, CvLog]:
        """Записи, относящиеся к текущему сценарию."""
        return {
            key: entry
            for key, entry in self.logs.items()
            if entry.scenario in (None, self._sop_id)
        }

    @property
    def has_recordings(self) -> bool:
        return bool(self.scenario_logs())

    def reset(self) -> None:
        self._held.clear()
        self._spoken_until.clear()
        self._last_event_at = None
        self._stream_lost = False
        self._translator.subjects.reset()
        self._translator.reset_frame_epoch()

    def status(self) -> dict:
        return {
            "enabled": self._config.enabled,
            "path": self._config.path,
            "clients": self._clients,
            "stream_lost": self._stream_lost,
            "held": sorted(self._held),
            "last_event_at": self._last_event_at.isoformat() if self._last_event_at else None,
            "replay": self.player.current.id if self.player.playing else None,
            "replay_position_sec": round(self.player.position_sec, 2),
            "replay_title": self.player.current.title if self.player.playing else None,
            "replay_run": self.player.run,
            "replay_holding": self.player.holding,
            "replay_video": bool(
                self.player.playing and self.player.current and self.player.current.video
            ),
            **self._translator.stats.as_dict(),
        }

    # ------------------------------------------------------------------
    # Приём
    # ------------------------------------------------------------------

    async def handle_message(self, raw: str) -> None:
        """Разобрать одно сообщение CV и опубликовать результат в шину.

        Разбор по полю event_type: два типа сообщений, смешивать их нельзя.
        Сообщение, не прошедшее контракт, логируется и пропускается — один
        кривой пакет от смежника не должен рвать соединение.
        """
        try:
            event = _parse(raw)
        except (ValidationError, ValueError) as exc:
            self._translator.stats.rejected += 1
            log.warning("сообщение CV не прошло контракт: %s", exc)
            return

        self._last_event_at = utc_now()

        if isinstance(event, ServiceEvent):
            health = self._translator.service(event)
            self._stream_lost = health.state is StreamState.LOST
            if self._stream_lost:
                # Данные больше не поступают — удерживать нечего, иначе
                # движок не увидит разницы между обрывом и спокойной сценой.
                self._held.clear()
            await self._bus.publish(CV_HEALTH, health)
            return

        message = self._translator.object_state(event)
        if message is None:
            return

        if isinstance(message, StreamHealth):
            self._stream_lost = True
            self._held.clear()
            await self._bus.publish(CV_HEALTH, message)
            return

        if self._stream_lost:
            # Пошли данные — значит поток восстановился, даже если отдельного
            # служебного события об этом не было.
            self._stream_lost = False
            await self._bus.publish(
                CV_HEALTH,
                StreamHealth(
                    source=message.source,
                    camera_id=message.camera_id,
                    state=StreamState.ONLINE,
                    reason="возобновились наблюдения от CV",
                ),
            )

        self._held[message.variable] = _Held(
            value=message.value,
            confidence=message.confidence,
            unclear=message.unclear,
            source=message.source,
            camera_id=message.camera_id,
            bbox=message.bbox,
        )
        await self._bus.publish(CV_OBSERVATIONS, message)

    # ------------------------------------------------------------------
    # Удержание
    # ------------------------------------------------------------------

    async def _republish_loop(self) -> None:
        period = 1.0 / self._config.republish_hz
        while True:
            try:
                await self._republish_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - приёмник не должен падать молча
                log.exception("сбой в такте удержания состояния")
            await asyncio.sleep(period)

    async def _republish_tick(self) -> None:
        if not self._held:
            return

        now = utc_now()
        # Молчание само по себе ничего не значит: события приходят только на
        # смену состояния, поэтому сотрудник, неподвижно стоящий в каске, не
        # порождает их вовсе. Сторожить молчание можно лишь там, где смежник
        # обещал периодический признак жизни; отвалившийся модуль ловится
        # разрывом соединения, а не этим таймером.
        #
        # Во время показа записи таймер не нужен и подавно: файл кончился там,
        # где кончилась последняя смена состояния.
        if (
            self._config.heartbeat_expected
            and self._last_event_at is not None
            and not self.player.active
        ):
            silence = (now - self._last_event_at).total_seconds()
            if silence >= self._config.hold_timeout_sec:
                log.warning("от CV нет событий %.1f с — удержание прекращено", silence)
                self._held.clear()
                self._stream_lost = True
                await self._bus.publish(
                    CV_HEALTH,
                    StreamHealth(
                        source=SERVICE_NAME,
                        camera_id="",
                        state=StreamState.LOST,
                        reason=f"от CV-модуля нет событий {silence:.0f} с",
                    ),
                )
                return

        for variable, held in list(self._held.items()):
            await self._bus.publish(
                CV_OBSERVATIONS,
                Observation(
                    source=held.source,
                    camera_id=held.camera_id,
                    frame_ts=now,
                    variable=variable,
                    value=held.value,
                    confidence=held.confidence,
                    unclear=held.unclear,
                    bbox=held.bbox,
                ),
            )

    # ------------------------------------------------------------------

    async def serve(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients += 1
        peer = websocket.client.host if websocket.client else "?"
        log.info("CV-модуль подключился (%s), клиентов: %d", peer, self._clients)
        try:
            while True:
                raw = await websocket.receive_text()
                if self.player.active:
                    # Идёт запись. Живой модуль по той же переменной спорил бы
                    # с ней, а его «нет камеры» оборвало бы показ.
                    continue
                await self.handle_message(raw)
        except WebSocketDisconnect:
            log.info("CV-модуль отключился (%s)", peer)
        finally:
            self._clients -= 1
            if self._clients == 0 and not self.player.active:
                # Соединения нет — данных не будет. Молчание отработает
                # сторожевой таймер удержания, а причину отключения
                # зафиксируем сразу.
                self._held.clear()
                self._stream_lost = True
                await self._bus.publish(
                    CV_HEALTH,
                    StreamHealth(
                        source=SERVICE_NAME,
                        camera_id="",
                        state=StreamState.LOST,
                        reason="CV-модуль разорвал соединение",
                    ),
                )


def _parse(raw: str) -> ObjectStateEvent | ServiceEvent:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("ожидался объект JSON")

    match payload.get("event_type"):
        case "object_state":
            return ObjectStateEvent.model_validate(payload)
        case "cv_service":
            return ServiceEvent.model_validate(payload)
        case other:
            raise ValueError(f"неизвестный event_type: {other!r}")


def create_router(resolve: Resolve, *, path: str) -> APIRouter:
    """Маршруты приёмника событий CV.

    Занятий на стенде несколько, поэтому служба не одна: `resolve` находит ту,
    что принадлежит зрителю этого запроса. Адрес сокета берётся из настроек:
    он один на стенд и известен до того, как поднято первое занятие.
    """
    router = APIRouter(tags=["cv"])

    @router.websocket(path)
    async def cv_socket(websocket: WebSocket) -> None:
        service = await resolve(websocket)
        await service.serve(websocket)

    @router.get("/cv/logs")
    async def cv_logs(request: Request) -> list[dict]:
        """Записи реальных прогонов CV-модуля, доступные для проигрывания.

        Пока источником служит эмулятор, записи не предлагаются: это
        альтернативные источники, а не дополняющие друг друга.
        """
        service = await resolve(request)
        return [
            {
                "id": entry.id,
                "title": entry.title,
                "description": entry.description,
                "duration_sec": round(entry.duration_sec, 1),
                "events": entry.events,
                "subjects": entry.subjects,
                "video": entry.video is not None,
            }
            for entry in service.scenario_logs().values()
        ]

    @router.get("/cv/logs/{log_id}/video")
    async def log_video(log_id: str, request: Request) -> FileResponse:
        """Ролик с отрисовкой распознавания для этой записи."""
        service = await resolve(request)
        entry = service.logs.get(log_id)
        if entry is None or entry.video is None:
            raise HTTPException(status_code=404, detail="ролик для записи не найден")
        return FileResponse(entry.video, media_type="video/mp4")

    @router.post("/cv/logs/{log_id}/play")
    async def play_log(log_id: str, request: Request) -> dict:
        service = await resolve(request)
        if log_id not in service.scenario_logs():
            raise HTTPException(status_code=404, detail=f"запись '{log_id}' не найдена")
        await service.play_log(log_id)
        return {"ok": True, "id": log_id}

    @router.post("/cv/logs/stop")
    async def stop_log(request: Request) -> dict:
        service = await resolve(request)
        service.player.stop()
        return {"ok": True}

    @router.get("/cv/status")
    async def cv_status(request: Request) -> dict:
        service = await resolve(request)
        return service.status()

    return router


__all__ = ["CvIngressService", "SERVICE_NAME", "create_router"]
