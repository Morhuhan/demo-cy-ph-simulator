"""Сервис хранилища: подписка на шину, журнал, отчёты, выгрузка CSV."""

from __future__ import annotations

from urllib.parse import quote

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Response

from libs.bus import (
    CV_OBSERVATIONS,
    SESSION_SNAPSHOTS,
    SPOKEN_EVENTS,
    STATE_CHANGES,
    VERDICTS,
    Bus,
)
from libs.config import AppConfig
from libs.debrief import compose_debrief

from .report_docx import build_report, file_name
from libs.contracts import (
    Observation,
    SessionSnapshot,
    SopSpec,
    SpokenEvent,
    StateChange,
    Verdict,
)

from .repository import Repository

log = logging.getLogger(__name__)

SERVICE_NAME = "storage_api"
CONSUMER_GROUP = "storage"

#: Сырые наблюдения пишутся пачками: по одной транзакции на сообщение при
#: пятнадцати сообщениях в секунду база захлебнётся на ровном месте.
OBSERVATION_BATCH = 100
OBSERVATION_FLUSH_SEC = 2.0


class StorageService:
    def __init__(
        self,
        bus: Bus,
        sop: SopSpec,
        config: AppConfig,
        sops: dict[str, SopSpec] | None = None,
        repository: Repository | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        # Журнал можно передать снаружи: занятий на стенде несколько, а база
        # одна, и держать на каждое занятие свой пул соединений незачем —
        # записи в ней и так разделены идентификатором сеанса.
        self.repository = repository or Repository(config.storage, sop, sops)
        self._tasks: list[asyncio.Task[None]] = []
        self._session_id: str | None = None
        self._observation_buffer: list[Observation] = []

    async def start(self) -> None:
        await self.repository.create_schema()
        self._tasks = [
            asyncio.create_task(self._consume_snapshots(), name="storage-sessions"),
            asyncio.create_task(self._consume_verdicts(), name="storage-verdicts"),
            asyncio.create_task(self._consume_states(), name="storage-states"),
            asyncio.create_task(self._consume_utterances(), name="storage-utterances"),
        ]
        if self._config.storage.store_observations:
            self._tasks.append(
                asyncio.create_task(self._consume_observations(), name="storage-observations")
            )
            self._tasks.append(
                asyncio.create_task(self._flush_observations(), name="storage-flush")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        await self.repository.close()

    async def _consume_snapshots(self) -> None:
        async for snapshot in self._bus.subscribe(
            SESSION_SNAPSHOTS, SessionSnapshot, group=CONSUMER_GROUP
        ):
            try:
                self._session_id = snapshot.session_id
                await self.repository.upsert_session(snapshot)
            except Exception:  # pragma: no cover
                log.exception("не удалось сохранить сеанс")

    async def _consume_verdicts(self) -> None:
        async for verdict in self._bus.subscribe(VERDICTS, Verdict, group=CONSUMER_GROUP):
            try:
                await self.repository.add_verdict(verdict)
            except Exception:  # pragma: no cover
                log.exception("не удалось сохранить вердикт")

    async def _consume_states(self) -> None:
        async for change in self._bus.subscribe(STATE_CHANGES, StateChange, group=CONSUMER_GROUP):
            try:
                await self.repository.add_state_change(change, self._session_id)
            except Exception:  # pragma: no cover
                log.exception("не удалось сохранить смену состояния")

    async def _consume_utterances(self) -> None:
        async for event in self._bus.subscribe(SPOKEN_EVENTS, SpokenEvent, group=CONSUMER_GROUP):
            try:
                await self.repository.add_utterance(event)
            except Exception:  # pragma: no cover
                log.exception("не удалось сохранить реплику")

    async def _consume_observations(self) -> None:
        async for observation in self._bus.subscribe(
            CV_OBSERVATIONS, Observation, group=CONSUMER_GROUP
        ):
            self._observation_buffer.append(observation)
            if len(self._observation_buffer) >= OBSERVATION_BATCH:
                await self._flush_now()

    async def _flush_observations(self) -> None:
        while True:
            await asyncio.sleep(OBSERVATION_FLUSH_SEC)
            await self._flush_now()

    async def _flush_now(self) -> None:
        if not self._observation_buffer:
            return
        batch, self._observation_buffer = self._observation_buffer, []
        try:
            await self.repository.add_observations(batch, self._session_id)
        except Exception:  # pragma: no cover
            log.exception("не удалось сохранить пачку наблюдений")


def create_router(repository: Repository, config: AppConfig) -> APIRouter:
    """Маршруты журнала занятий.

    Эти маршруты только читают базу, а база на стенде одна: занятия у зрителей
    разные, но записи в ней и так разделены идентификатором сеанса. Поэтому
    искать службу конкретного зрителя здесь незачем.
    """
    router = APIRouter(prefix="/api", tags=["storage"])

    @router.get("/sessions")
    async def sessions(limit: int = 50) -> list[dict]:
        rows = await repository.list_sessions(limit)
        return [
            {
                "id": row.id,
                "mode": row.mode,
                "state": row.state,
                "trainee_name": row.trainee_name,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "duration_sec": row.duration_sec,
                "errors_count": row.errors_count,
                "steps_done": row.steps_done,
                "steps_total": row.steps_total,
            }
            for row in rows
        ]

    @router.get("/sessions/{session_id}/verdicts")
    async def verdicts(session_id: str) -> list[dict]:
        rows = await repository.list_verdicts(session_id)
        return [
            {
                "id": row.id,
                "ts": row.ts,
                "step_id": row.step_id,
                "step_index": row.step_index,
                "kind": row.kind,
                "severity": row.severity,
                "reason_code": row.reason_code,
                "confidence": row.confidence,
                "message": row.message,
                "latency_ms": row.latency_ms,
            }
            for row in rows
        ]

    @router.get("/sessions/{session_id}/summary")
    async def summary(session_id: str) -> dict:
        row = await repository.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="сеанс не найден")
        return {
            "session": {
                "id": row.id,
                "mode": row.mode,
                "state": row.state,
                "trainee_name": row.trainee_name,
                "errors_count": row.errors_count,
                "duration_sec": row.duration_sec,
                "steps_done": row.steps_done,
                "steps_total": row.steps_total,
            },
            "verdicts": await repository.summary(session_id),
            "latency": await repository.latency_stats(session_id),
        }

    @router.get("/sessions/{session_id}/debrief")
    async def debrief(session_id: str) -> dict:
        """Разбор занятия связным текстом.

        Формируется по сохранённым вердиктам после занятия, поэтому языковая
        модель здесь допустима: в тракт принятия решений и в норматив задержки
        она не попадает.
        """
        data = await repository.debrief_input(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="сеанс не найден")

        result = await compose_debrief(config.debrief, data)
        return {
            "session_id": session_id,
            "text": result.text,
            "engine": result.engine,
            "fallback_reason": result.fallback_reason,
        }

    @router.get("/sessions/{session_id}/report.docx")
    async def session_report(session_id: str) -> Response:
        """Отчёт о занятии документом Word: сводка, разбор и журнал."""
        data = await repository.debrief_input(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="сеанс не найден")

        row = await repository.get_session(session_id)
        debrief = await compose_debrief(config.debrief, data)
        body = await asyncio.to_thread(
            build_report, data, debrief, started_at=row.started_at if row else None
        )

        name = quote(file_name(data, row.started_at if row else None))
        return Response(
            content=body,
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
        )

    @router.get("/report")
    async def report(limit: int = 200) -> list[dict]:
        return [row.__dict__ for row in await repository.report_rows(limit)]

    @router.get("/report.csv")
    async def report_csv(limit: int = 200) -> Response:
        body = await repository.report_csv(limit)
        # UTF-8 с BOM: без него Excel открывает кириллицу кракозябрами, а отчёт
        # по п. 5.4 ТЗ смотрят именно в Excel.
        return Response(
            content=body.encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="kft01-report.csv"'},
        )

    @router.get("/latency")
    async def latency(session_id: str | None = None) -> dict:
        stats = await repository.latency_stats(session_id)
        budget = config.latency_budget
        return {
            "budget_ms": {
                "cv": budget.cv_ms,
                "smoothing": budget.smoothing_ms,
                "engine": budget.engine_ms,
                "voice": budget.voice_ms,
                "total": budget.total_ms,
            },
            "measured": stats,
            "within_budget": stats["end_to_end"]["max_ms"] <= budget.total_ms,
        }

    @router.post("/retention")
    async def retention() -> dict:
        return await repository.apply_retention()

    return router
