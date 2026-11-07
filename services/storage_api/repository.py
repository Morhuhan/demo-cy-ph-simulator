"""Работа с базой: запись журнала, отчёт по п. 5.4 ТЗ, сроки хранения."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from libs.config import StorageConfig
from libs.debrief import DebriefEvent, DebriefInput
from libs.contracts import (
    Observation,
    SessionSnapshot,
    SopSpec,
    SpokenEvent,
    StateChange,
    StepStatus,
    Verdict,
    VerdictKind,
)

from .models import (
    Base,
    ObservationRow,
    SessionRow,
    StateChangeRow,
    UtteranceRow,
    VerdictRow,
)

log = logging.getLogger(__name__)


@dataclass
class ReportRow:
    """Строка отчёта по форме п. 5.4 ТЗ."""

    trainee_name: str
    mode: str
    scenario: str
    errors: int
    failed_steps: str
    date: str
    duration_min: float
    session_id: str
    state: str


#: Заголовки CSV — ровно по форме из ТЗ, чтобы выгрузку можно было
#: приложить к акту без переименований.
CSV_HEADERS = [
    "ФИО",
    "Режим",
    "Сценарий",
    "Ошибки",
    "Ошиб. шаги",
    "Дата",
    "Длит., мин.",
]

_MODE_TITLES = {"training": "Учебный", "testing": "Тестовый", "demo": "Демонстрационный"}


def _ensure_sqlite_dir(url: str) -> None:
    """Создать каталог под файл SQLite, если его ещё нет.

    Сам файл SQLite создаёт при первом подключении, а каталог — нет, и на
    свежей установке стенд падал бы при старте с «unable to open database
    file». В Docker каталог создаётся при сборке образа, локально его до сих
    пор просто не удаляли.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return
    Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


class Repository:
    def __init__(
        self,
        config: StorageConfig,
        sop: SopSpec,
        sops: dict[str, SopSpec] | None = None,
    ) -> None:
        self._config = config
        self._sop = sop
        # Отчёт охватывает занятия по разным сценариям: сценарий переключается
        # на ходу. Названия шагов поэтому ищутся в том СОП, по которому шло
        # занятие, а не в том, что загружен сейчас.
        self._sops = dict(sops or {})
        self._sops.setdefault(sop.id, sop)
        _ensure_sqlite_dir(config.url)
        self._engine = create_async_engine(config.url, future=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        """Создать схему, если её нет.

        На стенде это удобно, в поставке схемой управляет Alembic
        (`alembic upgrade head`) — см. docs/operations.md.
        """
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    def session(self) -> AsyncSession:
        return self._session_factory()

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    async def upsert_session(self, snapshot: SessionSnapshot) -> None:
        if snapshot.started_ts is None:
            # Сеанс, который не начинали, — это не тренировка, а состояние
            # покоя движка. В журнал и в отчёт по п. 5.4 ТЗ он попадать не должен.
            return

        steps_done = sum(1 for step in snapshot.steps if step.status is StepStatus.DONE)
        # Для незавершённого сеанса считаем длительность на текущий момент:
        # инструктору нужно видеть, сколько тренировка уже идёт.
        end = snapshot.finished_ts or datetime.now(UTC)
        duration = (end - snapshot.started_ts).total_seconds()

        async with self.session() as db:
            row = await db.get(SessionRow, snapshot.session_id)
            if row is None:
                row = SessionRow(id=snapshot.session_id)
                db.add(row)

            row.sop_id = snapshot.sop_id
            row.sop_title = snapshot.sop_title
            row.scenario_code = snapshot.scenario_code or self._sop.scenario_code
            row.mode = snapshot.mode.value
            row.state = snapshot.state.value
            row.trainee_name = snapshot.trainee_name
            row.started_at = snapshot.started_ts
            row.finished_at = snapshot.finished_ts
            row.duration_sec = duration
            row.errors_count = snapshot.errors_count
            row.failed_step_ids = list(snapshot.failed_step_ids)
            row.steps_total = len(snapshot.steps)
            row.steps_done = steps_done

            await db.commit()

    async def add_verdict(self, verdict: Verdict) -> None:
        async with self.session() as db:
            db.add(
                VerdictRow(
                    id=verdict.event_id,
                    session_id=verdict.session_id,
                    step_id=verdict.step_id,
                    step_index=verdict.step_index,
                    kind=verdict.kind.value,
                    severity=verdict.severity.value,
                    reason_code=verdict.reason_code.value,
                    confidence=verdict.confidence,
                    message=verdict.message,
                    details=verdict.details,
                    source_event_id=verdict.source_event_id,
                    latency_ms=verdict.latency_ms,
                    ts=verdict.ts,
                )
            )
            await db.commit()

    async def add_state_change(self, change: StateChange, session_id: str | None) -> None:
        async with self.session() as db:
            db.add(
                StateChangeRow(
                    id=change.event_id,
                    session_id=session_id,
                    variable=change.variable,
                    value=change.value,
                    previous_value=change.previous_value,
                    quality=change.quality.value,
                    confidence=change.confidence,
                    agreement=change.agreement,
                    observations=change.observations,
                    first_seen_ts=change.first_seen_ts,
                    confirmed_ts=change.confirmed_ts,
                    source=change.source,
                    camera_id=change.camera_id,
                    post_id=change.post_id,
                )
            )
            await db.commit()

    async def add_utterance(self, event: SpokenEvent) -> None:
        async with self.session() as db:
            db.add(
                UtteranceRow(
                    id=event.event_id,
                    session_id=event.session_id,
                    text=event.text,
                    kind=event.kind.value,
                    engine=event.engine,
                    cache_hit=event.cache_hit,
                    synth_ms=event.synth_ms,
                    end_to_end_ms=event.end_to_end_ms,
                    ts=event.ts,
                )
            )
            await db.commit()

    async def add_observations(
        self, observations: list[Observation], session_id: str | None
    ) -> None:
        if not self._config.store_observations or not observations:
            return
        async with self.session() as db:
            db.add_all(
                ObservationRow(
                    id=observation.event_id,
                    session_id=session_id,
                    source=observation.source,
                    camera_id=observation.camera_id,
                    post_id=observation.post_id,
                    variable=observation.variable,
                    value=observation.value,
                    confidence=observation.confidence,
                    unclear=observation.unclear,
                    frame_ts=observation.frame_ts,
                )
                for observation in observations
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    async def list_sessions(self, limit: int = 50) -> list[SessionRow]:
        async with self.session() as db:
            result = await db.execute(
                select(SessionRow).order_by(SessionRow.started_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def get_session(self, session_id: str) -> SessionRow | None:
        async with self.session() as db:
            return await db.get(SessionRow, session_id)

    async def list_verdicts(self, session_id: str) -> list[VerdictRow]:
        async with self.session() as db:
            result = await db.execute(
                select(VerdictRow)
                .where(VerdictRow.session_id == session_id)
                .order_by(VerdictRow.ts)
            )
            return list(result.scalars())

    async def debrief_input(self, session_id: str) -> DebriefInput | None:
        """Собрать занятие в вид, пригодный для составления разбора.

        Названия шагов и последствия берутся из того СОП, по которому шло
        занятие: отчёт охватывает разные сценарии, и подставлять текущий
        нельзя — получились бы чужие формулировки.
        """
        row = await self.get_session(session_id)
        if row is None:
            return None

        sop = self._sops.get(row.sop_id, self._sop)
        steps = {step.id: step for step in sop.steps}
        started = row.started_at

        events: list[DebriefEvent] = []
        for verdict in await self.list_verdicts(session_id):
            step = steps.get(verdict.step_id)
            at = (verdict.ts - started).total_seconds() if started and verdict.ts else 0.0
            events.append(
                DebriefEvent(
                    at_sec=max(0.0, at),
                    step_title=step.title if step else verdict.step_id,
                    kind=verdict.kind,
                    severity=verdict.severity,
                    reason=verdict.reason_code,
                    message=verdict.message,
                    consequence=step.consequence if step else None,
                )
            )

        return DebriefInput(
            trainee_name=row.trainee_name or "",
            scenario=f"{row.scenario_code} — {row.sop_title}".strip(" —"),
            mode=_MODE_TITLES.get(row.mode, row.mode),
            state=row.state,
            duration_sec=row.duration_sec or 0.0,
            steps_done=row.steps_done or 0,
            steps_total=row.steps_total or 0,
            errors=row.errors_count or 0,
            events=events,
        )

    async def latency_stats(self, session_id: str | None = None) -> dict:
        """Статистика задержки — то, что предъявляется по п. 6.2 ТЗ.

        Считается по двум точкам: вердикт (сценарная часть) и начало реплики
        (сквозная). Разделение нужно, чтобы на приёмке было видно, какое звено
        сколько израсходовало.
        """
        async with self.session() as db:
            verdict_query = select(
                func.count(VerdictRow.latency_ms),
                func.avg(VerdictRow.latency_ms),
                func.max(VerdictRow.latency_ms),
            ).where(VerdictRow.latency_ms.is_not(None))
            utterance_query = select(
                func.count(UtteranceRow.end_to_end_ms),
                func.avg(UtteranceRow.end_to_end_ms),
                func.max(UtteranceRow.end_to_end_ms),
            ).where(UtteranceRow.end_to_end_ms.is_not(None))

            if session_id:
                verdict_query = verdict_query.where(VerdictRow.session_id == session_id)
                utterance_query = utterance_query.where(UtteranceRow.session_id == session_id)

            verdict_count, verdict_avg, verdict_max = (
                await db.execute(verdict_query)
            ).one()
            speech_count, speech_avg, speech_max = (await db.execute(utterance_query)).one()

        return {
            "verdict": {
                "count": verdict_count or 0,
                "avg_ms": round(verdict_avg or 0.0, 1),
                "max_ms": round(verdict_max or 0.0, 1),
            },
            "end_to_end": {
                "count": speech_count or 0,
                "avg_ms": round(speech_avg or 0.0, 1),
                "max_ms": round(speech_max or 0.0, 1),
            },
        }

    async def report_rows(self, limit: int = 200) -> list[ReportRow]:
        sessions = await self.list_sessions(limit)
        rows: list[ReportRow] = []

        for row in sessions:
            sop = self._sops.get(row.sop_id, self._sop)
            titles = {step.id: step.title for step in sop.steps}
            failed = ", ".join(titles.get(sid, sid) for sid in (row.failed_step_ids or []))
            date = row.started_at.astimezone().strftime("%d.%m.%Y %H:%M") if row.started_at else ""
            rows.append(
                ReportRow(
                    trainee_name=row.trainee_name or "—",
                    mode=_MODE_TITLES.get(row.mode, row.mode),
                    scenario=f"{row.scenario_code} — {row.sop_title}",
                    errors=row.errors_count,
                    failed_steps=failed or "—",
                    date=date,
                    duration_min=round((row.duration_sec or 0.0) / 60.0, 1),
                    session_id=row.id,
                    state=row.state,
                )
            )
        return rows

    async def report_csv(self, limit: int = 200) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(CSV_HEADERS)
        for row in await self.report_rows(limit):
            writer.writerow(
                [
                    row.trainee_name,
                    row.mode,
                    row.scenario,
                    row.errors,
                    row.failed_steps,
                    row.date,
                    f"{row.duration_min:.1f}".replace(".", ","),
                ]
            )
        return buffer.getvalue()

    # ------------------------------------------------------------------

    async def apply_retention(self, now: datetime | None = None) -> dict[str, int]:
        """Удалить данные старше сроков хранения из конфигурации.

        Заготовка под 152-ФЗ: сроки заданы в configs/app.yaml, вызывать можно
        по расписанию. Разные сроки для разных сущностей не случайны — сырые
        наблюдения живут неделю, отчёты о тренировках квартал.
        """
        now = now or datetime.now(UTC)
        retention = self._config.retention
        removed: dict[str, int] = {}

        async with self.session() as db:
            sessions_before = now - timedelta(days=retention.sessions_days)
            result = await db.execute(
                delete(SessionRow).where(SessionRow.started_at < sessions_before)
            )
            removed["sessions"] = result.rowcount or 0

            observations_before = now - timedelta(days=retention.observations_days)
            result = await db.execute(
                delete(ObservationRow).where(ObservationRow.frame_ts < observations_before)
            )
            removed["observations"] = result.rowcount or 0

            result = await db.execute(
                delete(StateChangeRow).where(StateChangeRow.confirmed_ts < observations_before)
            )
            removed["state_changes"] = result.rowcount or 0

            await db.commit()

        log.info("сроки хранения применены: %s", removed)
        return removed

    async def summary(self, session_id: str) -> dict:
        """Свод по сеансу для экрана инструктора."""
        verdicts = await self.list_verdicts(session_id)
        by_kind: dict[str, int] = {}
        for verdict in verdicts:
            by_kind[verdict.kind] = by_kind.get(verdict.kind, 0) + 1

        return {
            "total": len(verdicts),
            "match": by_kind.get(VerdictKind.MATCH.value, 0),
            "mismatch": by_kind.get(VerdictKind.MISMATCH.value, 0),
            "unclear": by_kind.get(VerdictKind.UNCLEAR.value, 0),
            "timeout": by_kind.get(VerdictKind.TIMEOUT.value, 0),
        }
