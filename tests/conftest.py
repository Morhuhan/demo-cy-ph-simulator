from __future__ import annotations

from datetime import UTC, datetime

import pytest

from libs.config import load_config, load_sop
from services.cv_adapter.runs import load_runs

EPOCH = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture(scope="session")
def sop(config):
    return load_sop(config.sop_path)


@pytest.fixture(scope="session")
def runs(config, sop):
    # Только прогоны текущего сценария: прогон чужого СОП оперирует
    # переменными, которых в нём нет.
    return load_runs(config.emulator.runs_dir, scenario=sop.id)
