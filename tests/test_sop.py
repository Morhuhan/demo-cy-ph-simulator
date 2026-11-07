"""Проверки машиночитаемого СОП.

Ошибка в регламенте должна вылезать при загрузке, а не в середине тренировки.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from libs.contracts import SopSpec


def test_sop_loads(sop):
    assert sop.scenario_code == "КФТ-01"
    assert len(sop.steps) == 7


def test_three_critical_steps(sop):
    """Заземление, механизм прижатия и опускание трубы до дна — по регламенту."""
    critical = [step.id for step in sop.steps if step.critical]
    assert critical == ["grounding", "clamp", "pipe_bottom"]


def test_critical_steps_declare_consequence(sop):
    """У критического шага обязано быть описано последствие ошибки.

    Оно уходит в голос и в отчёт — без него предупреждение звучит как
    придирка, а не как объяснение опасности.
    """
    for step in sop.steps:
        if step.critical:
            assert step.consequence, f"шаг '{step.id}' критический, но без consequence"


def test_hatch_blocked_by_grounding(sop):
    """Ключевая блокировка: люк нельзя открывать без заземления."""
    hatch = next(step for step in sop.steps if step.id == "hatch")
    assert hatch.requires == ["grounding"]


def test_report_requires_clamp_and_bottom(sop):
    report = next(step for step in sop.steps if step.id == "report")
    assert set(report.requires) == {"clamp", "pipe_bottom"}


def test_report_is_voice_only(sop):
    """Седьмой шаг камерой не наблюдается — только речь."""
    report = next(step for step in sop.steps if step.id == "report")
    assert all(expectation.voice_intent is not None for expectation in report.expect)


def test_pipe_has_four_positions(sop):
    """Шаги 3, 4 и 6 — одна переменная в трёх разных значениях плюс исходное."""
    assert set(sop.observables["pipe_position"].values) == {
        "stowed",
        "over_hatch",
        "seated",
        "bottom",
    }


def test_every_step_has_timeout(sop):
    """Сторожевой таймер задаётся на каждый шаг индивидуально."""
    for step in sop.steps:
        assert step.timeout_sec, f"у шага '{step.id}' нет тайм-аута"


def test_winch_step_has_longest_timeout(sop):
    """Опускание лебёдкой длится минуты — общий тайм-аут дал бы ложный TIMEOUT."""
    timeouts = {step.id: step.timeout_sec for step in sop.steps}
    assert timeouts["pipe_bottom"] == max(timeouts.values())


def test_unknown_variable_rejected(sop):
    broken = sop.model_dump()
    broken["steps"][0]["expect"] = [{"variable": "нет_такой", "equals": "green"}]
    with pytest.raises(ValidationError, match="неизвестная переменная"):
        SopSpec.model_validate(broken)


def test_unknown_value_rejected(sop):
    broken = sop.model_dump()
    broken["steps"][0]["expect"] = [{"variable": "grounding_lamp", "equals": "фиолетовый"}]
    with pytest.raises(ValidationError, match="недопустимо"):
        SopSpec.model_validate(broken)


def test_forward_requirement_rejected(sop):
    """requires не может ссылаться на шаг, который идёт позже."""
    broken = sop.model_dump()
    broken["steps"][0]["requires"] = ["report"]
    with pytest.raises(ValidationError, match="не раньше"):
        SopSpec.model_validate(broken)
