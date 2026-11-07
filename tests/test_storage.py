"""Журнал тренировок и отчёт по форме п. 5.4 ТЗ."""

from __future__ import annotations

from datetime import timedelta

import pytest

from libs.config import StorageConfig
from libs.contracts import Mode, SessionSnapshot, SessionState, StepStatus, StepView
from services.storage_api.repository import CSV_HEADERS, Repository
from services.scenario_engine.engine import ScenarioEngine

from .conftest import EPOCH


@pytest.fixture
async def repository(sop, tmp_path):
    config = StorageConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}")
    repo = Repository(config, sop)
    await repo.create_schema()
    yield repo
    await repo.close()


def snapshot(sop, **overrides) -> SessionSnapshot:
    defaults = dict(
        session_id="s-1",
        sop_id=sop.id,
        sop_title=sop.title,
        mode=Mode.TRAINING,
        state=SessionState.FINISHED,
        trainee_name="Иванов И. И.",
        steps=[
            StepView(id=step.id, index=index + 1, title=step.title, status=StepStatus.DONE)
            for index, step in enumerate(sop.steps)
        ],
        errors_count=0,
        started_ts=EPOCH,
        finished_ts=EPOCH + timedelta(minutes=4, seconds=30),
    )
    return SessionSnapshot(**{**defaults, **overrides})


async def test_report_row_matches_tz_form(repository, sop):
    await repository.upsert_session(
        snapshot(sop, errors_count=2, failed_step_ids=["grounding", "clamp"])
    )
    rows = await repository.report_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row.trainee_name == "Иванов И. И."
    assert row.mode == "Учебный"
    assert row.scenario.startswith("КФТ-01")
    assert row.errors == 2
    # В отчёте — формулировки шагов из регламента, а не их идентификаторы.
    assert "заземления" in row.failed_steps
    assert row.duration_min == pytest.approx(4.5, abs=0.1)


async def test_idle_session_not_recorded(repository, sop):
    """Незапущенный сеанс — это состояние покоя движка, а не тренировка."""
    await repository.upsert_session(
        snapshot(sop, state=SessionState.IDLE, started_ts=None, finished_ts=None)
    )
    assert await repository.report_rows() == []


async def test_reset_does_not_erase_previous_session(repository, sop, config):
    """Сброс готовит следующую тренировку, а не стирает прошедшую."""
    engine = ScenarioEngine(sop, config.engine)
    started = engine.start(mode=Mode.TRAINING, trainee_name="Петров", now=EPOCH)
    await repository.upsert_session(started.snapshot)
    finished_id = engine.session_id

    after_reset = engine.reset()
    await repository.upsert_session(after_reset.snapshot)

    assert engine.session_id != finished_id
    rows = await repository.report_rows()
    assert [row.session_id for row in rows] == [finished_id]
    assert rows[0].trainee_name == "Петров"


async def test_running_session_has_duration(repository, sop):
    """Инструктору нужно видеть, сколько тренировка уже идёт."""
    await repository.upsert_session(
        snapshot(sop, state=SessionState.RUNNING, finished_ts=None)
    )
    rows = await repository.report_rows()
    assert rows[0].duration_min > 0


async def test_csv_headers_match_tz(repository, sop):
    await repository.upsert_session(snapshot(sop))
    body = await repository.report_csv()

    header = body.splitlines()[0]
    assert header == ";".join(CSV_HEADERS)
    # Десятичная запятая — иначе Excel в русской локали не распознаёт число.
    assert ";4,5" in body


async def test_database_directory_is_created(sop, tmp_path):
    """На свежей установке каталога под базу нет — стенд создаёт его сам."""
    database = tmp_path / "data" / "nested" / "stand.sqlite3"
    repo = Repository(StorageConfig(url=f"sqlite+aiosqlite:///{database}"), sop)
    try:
        await repo.create_schema()
    finally:
        await repo.close()

    assert database.exists()
