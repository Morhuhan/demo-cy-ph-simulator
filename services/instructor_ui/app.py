"""Интерфейс инструктора (п. 6.2 ТЗ) и пульт эмулятора CV.

Состояние в браузере не хранится: движок присылает полный снимок сеанса на
каждое изменение, интерфейс просто его перерисовывает. Так экран не может
разойтись с движком, даже если пропустит сообщение.

Пути `/emulator`, `/voice` и `/api` работают одинаково в обеих сборках. В
локальном запуске соответствующие роутеры примонтированы в это же приложение,
в docker-сборке запросы проксируются в соседние сервисы. Клиентский код от
способа сборки не зависит.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from libs.bus import (
    CV_OBSERVATIONS,
    SESSION_COMMANDS,
    SESSION_SNAPSHOTS,
    SPOKEN_EVENTS,
    STATE_CHANGES,
    VERDICTS,
    Bus,
)
from libs.config import AppConfig, data_dir, load_sops
from libs.contracts import (
    CommandName,
    Mode,
    Observation,
    SessionCommand,
    SessionSnapshot,
    SopSpec,
    SpokenEvent,
    StateChange,
    Verdict,
)

from .mentor import AskRequest, Mentor, MentorError

log = logging.getLogger(__name__)

#: Как маршрут находит службу своего зрителя. Занятий на стенде несколько,
#: ключ занятия лежит у зрителя в cookie, поднимает и хранит их реестр.
Resolve = Callable[[Request | WebSocket], Awaitable["InstructorUiService"]]

SERVICE_NAME = "instructor_ui"
CONSUMER_GROUP = "instructor-ui"

STATIC_DIR = Path(__file__).parent / "static"
EXPO_DIR = STATIC_DIR / "expo"


class Hub:
    """Рассылка событий шины во все открытые вкладки инструктора."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._last_snapshot: dict | None = None
        self._history: list[dict] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        # Догоняем новую вкладку: снимок состояния и последние события журнала,
        # иначе открытый посреди тренировки экран был бы пустым.
        #
        # Догон помечается флагом: эти события уже прожиты. Журнал их
        # отрисовывает, но произносить их заново нельзя — иначе обновление
        # страницы или переподключение сокета превращается в хор из всех
        # реплик занятия разом.
        if self._last_snapshot is not None:
            await websocket.send_json(self._last_snapshot)
        for message in self._history[-80:]:
            await websocket.send_json({**message, "replay": True})

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    @property
    def viewers(self) -> int:
        """Сколько вкладок сейчас открыто. По этому счёту видно, что занятие
        ещё кому-то нужно и закрывать его рано."""
        return len(self._clients)

    async def broadcast(self, message: dict, *, remember: bool = True) -> None:
        if message["type"] == "snapshot":
            self._last_snapshot = message
        elif remember:
            self._history.append(message)
            del self._history[:-200]

        dead: list[WebSocket] = []
        for client in self._clients:
            try:
                await client.send_json(message)
            except Exception:  # pragma: no cover - вкладку закрыли
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def clear_history(self) -> None:
        self._history.clear()


class InstructorUiService:
    def __init__(self, bus: Bus, sop: SopSpec, config: AppConfig) -> None:
        self._bus = bus
        self._sop = sop
        self._config = config
        self.hub = Hub()
        self._tasks: list[asyncio.Task[None]] = []
        #: Все доступные сценарии и переключатель между ними. Заполняются в
        #: точке сборки: она единственная знает про остальные сервисы, которым
        #: смену сценария тоже нужно применить.
        self.scenarios: dict[str, SopSpec] = {}
        self.on_switch = None

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume_snapshots(), name="ui-snapshots"),
            asyncio.create_task(self._consume_verdicts(), name="ui-verdicts"),
            asyncio.create_task(self._consume_spoken(), name="ui-spoken"),
            asyncio.create_task(self._consume_observations(), name="ui-observations"),
            asyncio.create_task(self._consume_states(), name="ui-states"),
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

    async def _consume_snapshots(self) -> None:
        async for snapshot in self._bus.subscribe(
            SESSION_SNAPSHOTS, SessionSnapshot, group=CONSUMER_GROUP
        ):
            await self.hub.broadcast(
                {"type": "snapshot", "payload": snapshot.model_dump(mode="json")}
            )

    async def _consume_verdicts(self) -> None:
        async for verdict in self._bus.subscribe(VERDICTS, Verdict, group=CONSUMER_GROUP):
            await self.hub.broadcast(
                {"type": "verdict", "payload": verdict.model_dump(mode="json")}
            )

    async def _consume_spoken(self) -> None:
        async for event in self._bus.subscribe(SPOKEN_EVENTS, SpokenEvent, group=CONSUMER_GROUP):
            await self.hub.broadcast({"type": "spoken", "payload": event.model_dump(mode="json")})

    async def _consume_observations(self) -> None:
        """Сырой поток наблюдений — в окно видеопотока.

        remember=False принципиально: наблюдений идут десятки в секунду, и в
        истории они за минуту вытеснили бы вердикты, которыми догоняется
        журнал вновь открытой вкладки. Лента живая, догонять её нечем и незачем.
        """
        async for observation in self._bus.subscribe(
            CV_OBSERVATIONS, Observation, group=CONSUMER_GROUP
        ):
            await self.hub.broadcast(
                {
                    "type": "observation",
                    "payload": {
                        "frame_ts": observation.frame_ts.isoformat(),
                        "variable": observation.variable,
                        "value": observation.value,
                        "confidence": round(observation.confidence, 3),
                        "unclear": observation.unclear,
                        "source": observation.source,
                        "camera_id": observation.camera_id,
                        "bbox": observation.bbox.model_dump() if observation.bbox else None,
                    },
                },
                remember=False,
            )

    async def _consume_states(self) -> None:
        """Подтверждённые смены состояния — их лента выделяет среди наблюдений."""
        async for change in self._bus.subscribe(
            STATE_CHANGES, StateChange, group=CONSUMER_GROUP
        ):
            await self.hub.broadcast(
                {
                    "type": "state",
                    "payload": {
                        "variable": change.variable,
                        "value": change.value,
                        "previous_value": change.previous_value,
                        "quality": change.quality,
                        "confidence": round(change.confidence, 3),
                        "agreement": round(change.agreement, 3),
                        "observations": change.observations,
                        "confirmed_ts": change.confirmed_ts.isoformat(),
                    },
                },
                remember=False,
            )

    def switch_scenario(self, sop: SopSpec) -> None:
        self._sop = sop
        self.hub.clear_history()

    async def send_command(self, command: SessionCommand) -> None:
        if command.command in (CommandName.START, CommandName.RESET):
            self.hub.clear_history()
        await self._bus.publish(SESSION_COMMANDS, command)


class StartRequest(BaseModel):
    mode: Mode = Mode.TRAINING
    trainee_name: str | None = None


class ExpoResult(BaseModel):
    """Протокол прохождения. Без имени: обучаемый анонимен."""

    steps_done: int = Field(ge=0, le=20)
    steps_total: int = Field(ge=1, le=20)
    errors: int = Field(ge=0)
    ppe_violations: int = Field(ge=0)
    asks: int = Field(ge=0)
    seconds: int = Field(ge=0)
    helmet_source: str = Field(default="cv", max_length=20)
    lesson: str | None = Field(default=None, max_length=40)
    mode: str | None = Field(default=None, max_length=20)
    #: Экзамен: сдан ли (без ошибок и нарушений СИЗ). В других режимах оценки нет.
    passed: bool | None = None


class ExpoControlUpdate(BaseModel):
    """Что меняет инструктор с пульта. Пустое поле — не трогать."""

    lesson: str | None = Field(default=None, max_length=40)
    mode: Literal["review", "training", "exam"] | None = None
    view: Literal["orbit", "eye"] | None = None
    assume_helmet: bool | None = None
    command: Literal["start", "reset", "pause", "resume", "demo"] | None = None


class ExpoStatus(BaseModel):
    """Что сейчас на экране стенда — для пульта инструктора."""

    model_config = ConfigDict(extra="ignore")

    phase: str = Field(max_length=20)
    lesson: str | None = Field(default=None, max_length=40)
    mode: str | None = Field(default=None, max_length=20)
    view: str | None = Field(default=None, max_length=20)
    step: int = Field(default=0, ge=0, le=50)
    steps_total: int = Field(default=0, ge=0, le=50)
    step_title: str = Field(default="", max_length=120)
    errors: int = Field(default=0, ge=0)
    asks: int = Field(default=0, ge=0)
    seconds: int = Field(default=0, ge=0)
    present: bool = False
    helmet: bool = False
    helmet_source: str = Field(default="", max_length=60)


class ExpoControl:
    """Пульт инструктора экрана тренажёра.

    Экран стенда и пульт — разные страницы, пульт можно открыть на планшете
    или поверх экрана. Связь через этот объект: пульт меняет занятие, режим,
    вид камеры и отдаёт команды, экран присылает своё состояние. Экран на
    стенде один, поэтому и пульт один на процесс, без привязки к cookie.
    """

    def __init__(self) -> None:
        self.state: dict = {"lesson": "top-fill", "mode": "review", "view": "orbit", "assume_helmet": None, "seq": 0}
        self.status: dict | None = None
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        await websocket.send_json({"type": "control", "state": self.state, "command": None})
        if self.status is not None:
            await websocket.send_json({"type": "status", "status": self.status})

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def update(self, change: ExpoControlUpdate) -> dict:
        fields = change.model_dump(exclude_none=True, exclude={"command"})
        self.state = {**self.state, **fields, "seq": self.state["seq"] + 1}
        await self._send({"type": "control", "state": self.state, "command": change.command})
        return self.state

    async def report(self, status: ExpoStatus) -> None:
        self.status = status.model_dump()
        await self._send({"type": "status", "status": self.status})

    async def _send(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for client in self._clients:
            try:
                await client.send_json(message)
            except Exception:  # pragma: no cover - вкладку закрыли
                dead.append(client)
        for client in dead:
            self._clients.discard(client)


expo_control = ExpoControl()


#: Фоновые задачи маршрутов: держим ссылку, пока не закончатся.
_background: set[asyncio.Task] = set()

#: Выставочный экран объясняет верхний налив — его регламент получает наставник.
EXPO_SOP = "kft-01"


@lru_cache(maxsize=1)
def _expo_sop() -> SopSpec:
    return load_sops()[EXPO_SOP]


#: Как называть голосовые условия шага на экране.
_VOICE_TITLES = {
    "ready_report": "доклад о готовности",
    "repeat": "повтор инструкции",
    "help": "запрос помощи",
}


def _expectation_text(expectation, sop: SopSpec) -> str:
    """Условие шага словами, а не идентификаторами.

    На экране инструктора должно быть написано «Защитная каска — надета», а не
    `helmet=on`: имена переменных — деталь описания сценария, и читать их
    инструктору незачем.
    """
    if expectation.voice_intent is not None:
        return _VOICE_TITLES.get(expectation.voice_intent, str(expectation.voice_intent))

    observable = sop.observables.get(expectation.variable)
    if observable is None:
        return expectation.describe()
    value = observable.values.get(expectation.equals)
    return f"{observable.title} — {value.title if value else expectation.equals}"


def create_router(resolve: Resolve) -> APIRouter:
    """Маршруты интерфейса.

    Занятий на стенде несколько, поэтому служба не одна: у каждого запроса
    своя — ту, что принадлежит зрителю. Находит её `resolve` по ключу занятия
    из cookie.
    """
    router = APIRouter(tags=["ui"])

    @router.get("/")
    async def index() -> FileResponse:
        # Страница одна и целиком собирается из этого файла, поэтому её нельзя
        # кэшировать: после обновления стенда инструктор увидел бы старый экран,
        # работающий против нового движка. Ревалидация одного файла ничего не стоит.
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @router.get("/check")
    async def check() -> FileResponse:
        """Экран показа проверки каски: живое видео, одна кнопка, подсказки.

        Полный интерфейс инструктора остаётся на `/`; здесь нет ничего, кроме
        того, что нужно человеку перед камерой, — нажать не туда нельзя.
        """
        return FileResponse(
            STATIC_DIR / "check.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @router.get("/panel")
    async def panel() -> FileResponse:
        """Пульт в отдельном окне: ручное управление и лимиты времени."""
        return FileResponse(
            STATIC_DIR / "panel.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @router.get("/stream")
    async def stream() -> FileResponse:
        """Разбор видеопотока в отдельном окне: что система читает из кадра."""
        return FileResponse(
            STATIC_DIR / "stream.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # --- экран тренажёра (/expo) -------------------------------------------
    # Своя логика прохождения у экрана в браузере; от стенда он берёт каску
    # (сокет /ws), распознавание речи (/voice/listen) и ответы наставника.

    @router.get("/expo")
    async def expo_root() -> RedirectResponse:
        # Страница грузит сцену и модели относительными путями — нужен слэш.
        return RedirectResponse("/expo/")

    @router.get("/expo/")
    async def expo() -> FileResponse:
        """Выставочный экран: 3D-эстакада, каска с камеры, голосовой наставник."""
        return FileResponse(
            EXPO_DIR / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @router.get("/expo/state")
    async def expo_state(request: Request) -> dict:
        service = await resolve(request)
        mentor = Mentor(service._config.mentor)
        # Экран спрашивает состояние раз в минуту — заодно держим соединение тёплым.
        task = asyncio.create_task(mentor.warm())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return {"mentor": mentor.available(), "models": mentor.models if mentor.available() else []}

    @router.post("/expo/ask")
    async def expo_ask(body: AskRequest, request: Request) -> dict:
        """Вопрос обучаемого — языковой модели с контекстом этапа."""
        service = await resolve(request)
        mentor = Mentor(service._config.mentor)
        sop = service.scenarios.get(EXPO_SOP) or _expo_sop()
        try:
            answer, model = await mentor.ask(sop, body)
        except MentorError as exc:
            log.warning("наставник: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"answer": answer, "model": model}

    @router.post("/expo/result")
    async def expo_result(body: ExpoResult) -> dict:
        """Протокол прохождения — в журнал, по строке на прохождение."""
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **body.model_dump()}
        path = data_dir() / "expo-results.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        with path.open(encoding="utf-8") as fh:
            count = sum(1 for _ in fh)
        return {"saved": True, "count": count}

    @router.get("/expo/results")
    async def expo_results(limit: int = 30) -> dict:
        """Журнал прохождений для пульта: последние сверху."""
        path = data_dir() / "expo-results.jsonl"
        if not path.exists():
            return {"count": 0, "items": []}
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:  # строка, оборванная при сбое записи
                continue
        return {"count": len(rows), "items": rows[::-1][: max(1, min(limit, 200))]}

    @router.get("/expo/instructor")
    async def expo_instructor() -> FileResponse:
        """Пульт инструктора: занятие, режим, вид камеры, ход прохождения."""
        return FileResponse(
            EXPO_DIR / "instructor.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @router.get("/expo/control")
    async def expo_control_state() -> dict:
        return {"state": expo_control.state, "status": expo_control.status}

    @router.post("/expo/control")
    async def expo_control_update(body: ExpoControlUpdate) -> dict:
        return {"state": await expo_control.update(body)}

    @router.post("/expo/status")
    async def expo_status(body: ExpoStatus) -> dict:
        await expo_control.report(body)
        return {"ok": True}

    @router.websocket("/expo/live")
    async def expo_live(websocket: WebSocket) -> None:
        await expo_control.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            expo_control.disconnect(websocket)
        except Exception:  # pragma: no cover
            expo_control.disconnect(websocket)

    # Связь пульта и экрана обучаемых (expo/concept.html): стенд пересылает
    # сообщение всем остальным, поэтому окна могут быть в разных браузерах
    # и на разных компьютерах.
    concept_clients: set[WebSocket] = set()

    @router.websocket("/expo/concept/live")
    async def expo_concept_live(websocket: WebSocket) -> None:
        await websocket.accept()
        concept_clients.add(websocket)
        try:
            while True:
                text = await websocket.receive_text()
                for other in list(concept_clients):
                    if other is websocket:
                        continue
                    try:
                        await other.send_text(text)
                    except Exception:  # pragma: no cover
                        concept_clients.discard(other)
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover
            pass
        finally:
            concept_clients.discard(websocket)

    @router.get("/expo/{path:path}")
    async def expo_asset(path: str) -> FileResponse:
        """Сцена, модели и three.js. Модели не меняются — их можно кэшировать."""
        file = (EXPO_DIR / path).resolve()
        if not file.is_relative_to(EXPO_DIR.resolve()) or not file.is_file():
            raise HTTPException(status_code=404)
        cache = "max-age=86400" if "/models/" in f"/{path}" or path.startswith("vendor/") else "no-cache"
        return FileResponse(file, headers={"Cache-Control": cache})

    @router.get("/scenarios")
    async def scenarios(request: Request) -> list[dict]:
        """Сюжеты, между которыми можно переключаться на стенде."""
        service = await resolve(request)
        return [
            {
                "id": sop.id,
                "scenario_code": sop.scenario_code,
                "title": sop.title,
                "steps": len(sop.steps),
                "active": sop.id == service._sop.id,
            }
            for sop in service.scenarios.values()
        ]

    @router.post("/scenarios/{sop_id}")
    async def switch_scenario(sop_id: str, request: Request) -> dict:
        service = await resolve(request)
        if sop_id not in service.scenarios:
            raise HTTPException(status_code=404, detail=f"сценарий '{sop_id}' не найден")
        if service.on_switch is None:
            raise HTTPException(status_code=409, detail="переключение сценариев не подключено")
        if sop_id != service._sop.id:
            await service.on_switch(sop_id)
        return {"ok": True, "id": sop_id}

    @router.get("/scenario")
    async def scenario(request: Request) -> dict:
        """Описание сценария для экрана: шаги, наблюдаемые переменные, бюджет."""
        service = await resolve(request)
        sop = service._sop
        budget = service._config.latency_budget
        return {
            "id": sop.id,
            "scenario_code": sop.scenario_code,
            "title": sop.title,
            "description": sop.description,
            "source_document": sop.source_document,
            "steps": [
                {
                    "id": step.id,
                    "index": index + 1,
                    "title": step.title,
                    "critical": step.critical,
                    "requires": step.requires,
                    "timeout_sec": step.timeout_sec,
                    "consequence": step.consequence,
                    "instruction": step.phrases.instruction,
                    "expect": [_expectation_text(expectation, sop) for expectation in step.expect],
                }
                for index, step in enumerate(sop.steps)
            ],
            "observables": {
                name: {
                    "title": observable.title,
                    "description": observable.description,
                    "initial": observable.initial,
                    "values": {
                        value: meta.title for value, meta in observable.values.items()
                    },
                }
                for name, observable in sop.observables.items()
            },
            "latency_budget_ms": {
                "cv": budget.cv_ms,
                "smoothing": budget.smoothing_ms,
                "engine": budget.engine_ms,
                "voice": budget.voice_ms,
                "total": budget.total_ms,
            },
        }

    @router.post("/session/start")
    async def start_session(body: StartRequest, request: Request) -> dict:
        service = await resolve(request)
        await service.send_command(
            SessionCommand(
                command=CommandName.START,
                mode=body.mode,
                trainee_name=body.trainee_name,
            )
        )
        return {"ok": True}

    @router.post("/session/stop")
    async def stop_session(request: Request) -> dict:
        service = await resolve(request)
        await service.send_command(SessionCommand(command=CommandName.STOP))
        return {"ok": True}

    @router.post("/session/pause")
    async def pause_session(request: Request) -> dict:
        service = await resolve(request)
        await service.send_command(SessionCommand(command=CommandName.PAUSE))
        return {"ok": True}

    @router.post("/session/resume")
    async def resume_session(request: Request) -> dict:
        service = await resolve(request)
        await service.send_command(SessionCommand(command=CommandName.RESUME))
        return {"ok": True}

    @router.post("/session/reset")
    async def reset_session(request: Request) -> dict:
        service = await resolve(request)
        await service.send_command(SessionCommand(command=CommandName.RESET))
        return {"ok": True}

    @router.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        service = await resolve(websocket)
        await service.hub.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            service.hub.disconnect(websocket)
        except Exception:  # pragma: no cover
            service.hub.disconnect(websocket)

    return router


def attach_proxy(app: FastAPI, prefix: str, target: str) -> None:
    """Проброс префикса в соседний сервис — нужен только в docker-сборке.

    В локальном запуске роутеры примонтированы напрямую, и проксировать нечего.
    """
    # trust_env=False: сервисы стоят в одной сети compose, ходить между ними
    # через прокси из переменных окружения нельзя — в изолированном контуре
    # Заказчика такой запрос просто повиснет.
    client = httpx.AsyncClient(base_url=target.rstrip("/"), timeout=10.0, trust_env=False)

    @app.api_route(
        f"{prefix}/{{path:path}}",
        methods=["GET", "POST", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def proxy(path: str, request: Request) -> Response:
        url = f"{prefix}/{path}"
        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            content=await request.body(),
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )
